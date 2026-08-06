"""
gateway — backend-for-frontend / API gateway.

The Next.js portal talks only to this service; it fans out to the internal
FastAPI services and owns login/sessions.

Inherited shortcomings (left as-is from the handoff):
  * Records fan-out forwards the caller's session but never binds it to the
    {patient_id} being requested — any logged-in user can read any chart (IDOR).
  * Sessions never expire (see security.create_session / auth.yaml).
  * One role for everyone; no per-action authorization beyond "is logged in".
"""
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from config import settings
from db import get_db
from logging_config import configure
from models import User
from security import create_session, destroy_session, get_session, verify_password

log = configure(settings.service_name)

_MIN_INTERNAL_TOKEN_LENGTH = 32  # matches intake-service/records-service's own floor


def _internal_token_is_configured() -> bool:
    """Round-13 review (2026-08-06, PR #20): the gateway forwards
    X-Internal-Token on every intake/records call (see proxy_intake and the
    patient-view fan-out below) but never checked its own configured value
    before this fix — an empty/placeholder INTERNAL_SERVICE_TOKEN meant the
    gateway started and passed docker-compose's healthcheck while every
    forwarded call was fail-closed 401'd downstream, a healthy-looking
    outage of the whole intake/patient-view path. /healthz now applies the
    same presence/length floor intake-service and records-service already
    enforce on the receiving end."""
    configured = settings.internal_service_token
    return bool(configured) and len(configured) >= _MIN_INTERNAL_TOKEN_LENGTH


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Round-17 review (2026-08-06): the round-13 healthz check above only
    surfaces a missing/placeholder INTERNAL_SERVICE_TOKEN once something
    polls /healthz — a container starts, sits "unhealthy" until the
    healthcheck's retry budget expires, and the actual cause is buried
    behind a generic health-check failure with no message in
    `docker compose ps`. This fails at process startup instead: uvicorn logs
    the exact RuntimeError below and exits non-zero immediately (verified:
    `docker compose ps`/`logs` shows "Exited", not a slow unhealthy churn),
    so a misconfigured deploy fails as loudly and as early as possible.
    Does not fire under this repo's TestClient(app).get(...) pattern (no
    `with` block — Starlette only runs lifespan startup/shutdown for a
    context-managed TestClient), so it cannot break existing tests; it only
    ever runs for a real uvicorn-started process."""
    if not _internal_token_is_configured():
        raise RuntimeError(
            f"INTERNAL_SERVICE_TOKEN is not set (or is shorter than "
            f"{_MIN_INTERNAL_TOKEN_LENGTH} chars) — refusing to start. Set a real "
            f"random value (e.g. `openssl rand -hex 32`) in .env; see .env.example."
        )
    yield


app = FastAPI(title="Riverbend gateway", version="1.4.0", lifespan=lifespan)

SERVICES = {
    "intake": settings.intake_url,
    "eligibility": settings.eligibility_url,
    "records": settings.records_url,
    "scheduling": settings.scheduling_url,
    "interop": settings.interop_url,
    "roi": settings.roi_url,
}


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    username: str
    password: str


def _bearer(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    return authorization[7:] if authorization.lower().startswith("bearer ") else authorization


def require_session(authorization: Optional[str] = Header(default=None)) -> dict:
    """Reject anonymous callers. (Does NOT scope access to a patient — see IDOR.)"""
    sess = get_session(_bearer(authorization))
    if not sess:
        raise HTTPException(status_code=401, detail="not authenticated")
    return sess


@app.get("/healthz")
def healthz():
    if not _internal_token_is_configured():
        raise HTTPException(status_code=503, detail="internal_service_token not configured")
    return {"status": "ok", "service": settings.service_name}


@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """
    Issue a session token. Password only (no MFA), and the token never expires
    (no TTL on the Redis key) — see auth.yaml.
    """
    try:
        user = db.execute(select(User).where(User.username == req.username)).scalar_one_or_none()
    except Exception as e:  # DB down in local dev without compose
        log.error("login db error: %s", e)
        raise HTTPException(status_code=503, detail="auth backend unavailable")

    if not user or not user.is_active or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid username or password")

    user.last_login_at = func.now()
    db.commit()
    token = create_session(user.username, user.role)
    log.info("login ok user=%s", user.username)
    return {
        "token": token,
        "mfa": False,
        "user": {"username": user.username, "full_name": user.full_name, "role": user.role},
    }


@app.post("/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    destroy_session(_bearer(authorization))
    return {"status": "ok"}


@app.get("/me")
def me(session: dict = Depends(require_session)):
    return {"username": session.get("username"), "role": session.get("role")}


# --------------------------------------------------------------------------- #
# intake / eligibility
# --------------------------------------------------------------------------- #
@app.post("/intake")
def proxy_intake(payload: dict, session: dict = Depends(require_session)):
    # PR #20 round-8 review: forward_status=True — the old default silently
    # flattened intake-service's 409 duplicate-patient response into a bare
    # 200, so the frontend read it as a successful submission with no
    # patient/coverage/consent rows actually created.
    #
    # Round-11 review: X-Internal-Token proves this call came through the
    # gateway (require_session above already gated it on a real staff
    # session) rather than a direct caller hitting intake-service's
    # published host port, which had no way to tell the two apart and let
    # an unauthenticated caller probe the duplicate-detection response as a
    # patient/SSN-existence oracle. Same shared secret as proxy_patient_view.
    return _post(
        "intake", "/intake", payload, headers={"X-Internal-Token": settings.internal_service_token},
        forward_status=True,
    )


@app.get("/eligibility")
def proxy_eligibility(insurance_id: str, session: dict = Depends(require_session)):
    return _get("eligibility", "/eligibility", params={"insurance_id": insurance_id})


# --------------------------------------------------------------------------- #
# Stage 3: async eligibility job status/retry + visit-scoped assistant turns
#
# Same auth posture as every other route here: Depends(require_session) only
# — no new unauthenticated internal-service exposure is introduced. These
# routes carry the SAME limitation as the rest of the gateway: a valid
# session is required, but it is never checked against the specific
# job_id/visit_id being requested (see the IDOR note on proxy_records above)
# because every account maps to the single flat "staff" role
# (config/roles.yaml) — there is no per-action authorization to scope this
# to. That is documented, existing debt (RIV-201), not something Stage 3
# widens or attempts to fix.
# --------------------------------------------------------------------------- #
@app.get("/eligibility/jobs/{job_id}")
def proxy_eligibility_job_status(job_id: str, session: dict = Depends(require_session)):
    return _get(
        "eligibility", f"/eligibility/jobs/{job_id}", headers=_correlation_headers(), forward_status=True
    )


@app.post("/eligibility/jobs/{job_id}/retry")
def proxy_eligibility_job_retry(job_id: str, session: dict = Depends(require_session)):
    return _post(
        "eligibility", f"/eligibility/jobs/{job_id}/retry", {}, headers=_correlation_headers(), forward_status=True
    )


@app.post("/visits/{visit_id}/messages")
def proxy_visit_message(visit_id: str, payload: dict, session: dict = Depends(require_session)):
    return _post(
        "eligibility",
        f"/visits/{visit_id}/messages",
        payload,
        headers=_correlation_headers(),
        forward_status=True,
    )


# --------------------------------------------------------------------------- #
# patients / records
# --------------------------------------------------------------------------- #
@app.get("/patients")
def proxy_patients(
    session: dict = Depends(require_session),
    q: Optional[str] = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return _get("records", "/patients", params={"q": q, "limit": limit, "offset": offset})


@app.get("/patients/{patient_id}")
def proxy_patient(patient_id: int, session: dict = Depends(require_session)):
    return _get("records", f"/patients/{patient_id}")


@app.get("/patients/{patient_id}/records")
def proxy_records(patient_id: int, session: dict = Depends(require_session)):
    # IDOR: a valid session is required, but it is never checked against
    # {patient_id}. {patient_id} is the sequential primary key.
    return _get("records", f"/patients/{patient_id}/records")


@app.get("/records/search")
def proxy_search(q: str, session: dict = Depends(require_session)):
    return _get("records", "/records/search", params={"q": q})


# --------------------------------------------------------------------------- #
# Stage 3: bounded, evidence-cited patient-view agent (libs/patient_view_agent)
#
# `X-Actor-Id` carries the session's username to records-service, which uses
# it as `StaffAccessGate`'s deny-by-default check: an authenticated-staff
# access gate, NOT patient-specific authorization. It is DIFFERENT from the
# IDOR posture of proxy_records/proxy_patient above: an unauthenticated
# caller (no session at all) is still rejected here by require_session (401),
# same as everywhere else on this gateway, and now the access itself is
# recorded in a real audit_logs row (see records-service/app.py). What it
# does NOT do is check that this specific actor may see this specific
# patient_id — that ownership/care-team fact does not exist anywhere in this
# schema (users has no relationship to patients — see
# docs/analysis/RIV-201-patient-records-IDOR.md §6). This route is additive;
# it does not fix RIV-201 and proxy_records/proxy_patient remain exactly as
# exploitable as documented above.
#
# Review fix (round, 2026-08-05): `X-Actor-Id` by itself is only trustworthy
# if the request is guaranteed to have come from this gateway — but
# records-service's port is published to the host (docker-compose.yml), so a
# direct caller could spoof it. `X-Internal-Token` is a shared secret
# (INTERNAL_SERVICE_TOKEN) records-service verifies before honoring
# X-Actor-Id at all; records-service fails closed if it's unset.
# --------------------------------------------------------------------------- #
@app.get("/patients/{patient_id}/view")
def proxy_patient_view(
    patient_id: int,
    purpose: Optional[str] = None,
    session: dict = Depends(require_session),
):
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get(
        "records",
        f"/patients/{patient_id}/view",
        params={"purpose": purpose} if purpose else None,
        headers=headers,
        forward_status=True,
    )


# --------------------------------------------------------------------------- #
# scheduling
# --------------------------------------------------------------------------- #
@app.get("/slots")
def proxy_slots(
    session: dict = Depends(require_session),
    provider_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
):
    return _get("scheduling", "/slots", params={"provider_id": provider_id, "limit": limit})


@app.get("/appointments")
def proxy_list_appointments(patient_id: int, session: dict = Depends(require_session)):
    return _get("scheduling", "/appointments", params={"patient_id": patient_id})


@app.post("/appointments")
def proxy_book(payload: dict, session: dict = Depends(require_session)):
    return _post("scheduling", "/appointments", payload)


@app.post("/appointments/{appointment_id}/cancel")
def proxy_cancel(appointment_id: int, session: dict = Depends(require_session)):
    return _post("scheduling", f"/appointments/{appointment_id}/cancel", {})


# --------------------------------------------------------------------------- #
# release of information
# --------------------------------------------------------------------------- #
@app.get("/roi/requests")
def proxy_roi_list(session: dict = Depends(require_session), patient_id: Optional[int] = None):
    return _get("roi", "/roi/requests", params={"patient_id": patient_id})


@app.post("/roi/requests")
def proxy_roi_create(payload: dict, session: dict = Depends(require_session)):
    return _post("roi", "/roi/requests", payload)


@app.post("/roi/requests/{request_id}/fulfill")
def proxy_roi_fulfill(request_id: int, session: dict = Depends(require_session)):
    return _post("roi", f"/roi/requests/{request_id}/fulfill", {})


# --------------------------------------------------------------------------- #
# interop
# --------------------------------------------------------------------------- #
@app.post("/hl7/ingest")
def proxy_hl7(payload: dict, session: dict = Depends(require_session)):
    return _post("interop", "/hl7/ingest", payload)


# --------------------------------------------------------------------------- #
# transport helpers
# --------------------------------------------------------------------------- #
def _clean(params: Optional[dict]) -> dict:
    return {k: v for k, v in (params or {}).items() if v is not None}


def _correlation_headers() -> dict:
    # A safe, opaque correlation id (mirrors session tokens in security.py's
    # own uuid4().hex) — never derived from the session, a patient id, or any
    # other identifier — forwarded so intake-service/eligibility-service can
    # tie their own spans/logs for this request together.
    return {"X-Request-Id": uuid.uuid4().hex}


def _post(service: str, path: str, payload: dict, *, headers: Optional[dict] = None, forward_status: bool = False):
    try:
        r = httpx.post(f"{SERVICES[service]}{path}", json=payload, headers=headers, timeout=30)
        data = _safe_json(r)
        if forward_status:
            return JSONResponse(status_code=r.status_code, content=data)
        return data
    except Exception as e:
        log.error("proxy POST %s%s failed: %s", service, path, e)
        if forward_status:
            return JSONResponse(status_code=502, content={"error": str(e)})
        return {"error": str(e)}


def _get(
    service: str,
    path: str,
    params: Optional[dict] = None,
    *,
    headers: Optional[dict] = None,
    forward_status: bool = False,
):
    try:
        r = httpx.get(f"{SERVICES[service]}{path}", params=_clean(params), headers=headers, timeout=30)
        data = _safe_json(r)
        if forward_status:
            return JSONResponse(status_code=r.status_code, content=data)
        return data
    except Exception as e:
        log.error("proxy GET %s%s failed: %s", service, path, e)
        if forward_status:
            return JSONResponse(status_code=502, content={"error": str(e)})
        return {"error": str(e)}


def _safe_json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}
