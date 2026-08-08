"""
gateway — backend-for-frontend / API gateway.

The Next.js portal talks only to this service; it fans out to the internal
FastAPI services and owns login/sessions.

Inherited shortcomings (left as-is from the handoff):
  * Sessions never expire (see security.create_session / auth.yaml).
  * One role for everyone; no per-action authorization beyond "is logged in".

Week 4 catch-up: the RIV-201 IDOR ("Records fan-out forwards the caller's
session but never binds it to the {patient_id} being requested") is fixed
for `/patients/{id}` and `/patients/{id}/records` — see proxy_patient/
proxy_records below and services/records-service/patient_access_gate.py.
`/records/search` is scoped to the caller's authorized patients rather than
denying outright, since it returns a filtered list, not a single patient's
detail — see proxy_search below. `/patients` (roster browse/name search) is
a deliberate exception: front desk needs to find/register a patient before
any patient-specific grant could exist for them, so it stays gated on
"authenticated staff" only, same as before — see proxy_patients below and
the PR's "Open decisions" section.
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
    """Reject anonymous callers.

    PR #23 review round 2 (2026-08-07): the session now carries the stable
    users.id (forwarded downstream as X-Actor-Id) and expires via a Redis idle
    TTL that get_session refreshes on each read (security.py), so an abandoned
    token no longer lives forever. Per-request re-validation of users.is_active
    for chart data happens at the authorization boundary itself —
    records-service's SqlPatientAccessGate joins users.is_active, so a disabled
    account cannot read any patient chart even with a still-live session (login
    also rejects inactive users up front). That join, not a DB round-trip on
    every gateway call, is the central revocation point for PHI access.
    """
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
    Issue a session token. Password only (no MFA). Login rejects inactive
    users up front (below); the token carries the stable users.id and a Redis
    idle TTL (settings.session_timeout_seconds) refreshed on each request —
    see create_session. Per-request is_active revalidation for chart data is
    enforced at records-service's SqlPatientAccessGate (PR #23 review round 2).
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
    token = create_session(user.id, user.username, user.role)
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
    #
    # PR #23 (Week 4 catch-up): forward the authenticated actor so intake can
    # grant the registrar immediate access to the chart they create (front-desk
    # registration). X-Actor-Id is the stable users.id; X-Actor-Name is
    # username for audit only. Both are only trustworthy behind X-Internal-Token
    # (records/intake fail closed without it).
    headers = {
        "X-Internal-Token": settings.internal_service_token,
        "X-Actor-Id": session.get("user_id") or "",
        "X-Actor-Name": session.get("username") or "",
    }
    return _post("intake", "/intake", payload, headers=headers, forward_status=True)


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
    # PR #23 review round 2 (2026-08-07): GET /patients is now patient-scoped —
    # records-service filters the roster/name-search to the caller's active
    # grants (finding 2). This proxy MUST forward the actor, or records-service
    # sees no actor and returns an empty PatientPage, so every real user gets
    # total=0 even with active grants (the review's high finding: the failure is
    # silent — an empty page looks like success). X-Actor-Id is the stable
    # users.id used for the grant filter; X-Actor-Name is username for audit
    # only; X-Internal-Token proves the call came via the gateway (records-
    # service's port is published to the host). forward_status=True so a 401
    # (missing/misconfigured token) reaches the frontend, not a flattened 200.
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get(
        "records", "/patients", params={"q": q, "limit": limit, "offset": offset},
        headers=headers, forward_status=True,
    )


# Week 4 catch-up: patient/records reads now carry the same two headers as
# proxy_patient_view below — X-Internal-Token (proves the call came via this
# gateway, not a direct caller hitting records-service's published host
# port) and X-Actor-Id (the session's username, which records-service's new
# SqlPatientAccessGate checks against a real per-(actor, patient) grant
# BEFORE any patient lookup — see docs/analysis/RIV-201-patient-records-
# IDOR.md and services/records-service/patient_access_gate.py). This is the
# actual RIV-201 fix, not the defense-in-depth-only /view route: a caller
# without a grant for {patient_id} now gets 403, not the patient's data.
# forward_status=True so that 401/403/404/503 from records-service reach the
# frontend as-is instead of being silently flattened into a 200.
@app.get("/patients/{patient_id}")
def proxy_patient(patient_id: int, session: dict = Depends(require_session)):
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get("records", f"/patients/{patient_id}", headers=headers, forward_status=True)


@app.get("/patients/{patient_id}/records")
def proxy_records(patient_id: int, session: dict = Depends(require_session)):
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get("records", f"/patients/{patient_id}/records", headers=headers, forward_status=True)


@app.get("/records/search")
def proxy_search(q: str, session: dict = Depends(require_session)):
    # Week 4 catch-up: unlike /patients above, this returns actual clinical
    # note content (record bodies/snippets) across the whole patient
    # population, keyed only by a free-text guess — an alternate IDOR path
    # if left unscoped. records-service now filters results to the caller's
    # authorized patients using the same grant table as /patients/{id} and
    # /patients/{id}/records (see authorized_patient_ids in
    # patient_access_gate.py). A denied/unauthorized match is silently
    # dropped, never surfaced as a count or placeholder.
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get("records", "/records/search", params={"q": q}, headers=headers, forward_status=True)


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
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get(
        "records",
        f"/patients/{patient_id}/view",
        params={"purpose": purpose} if purpose else None,
        headers=headers,
        forward_status=True,
    )


# Stage 2 (Week 6) — read-only "possible duplicate patient" reconciliation
# view. Same trust model as proxy_patient_view above (X-Actor-Id +
# X-Internal-Token); this route is additive and does not fix RIV-201 either.
# No `purpose` param — records-service always requests Purpose.TREATMENT for
# this endpoint.
@app.get("/patients/{patient_id}/reconciliation")
def proxy_patient_reconciliation(
    patient_id: int,
    session: dict = Depends(require_session),
):
    # Consolidation review (PR #22): records-service scopes reconciliation to the
    # caller's grants keyed on the stable users.id — forward X-Actor-Id=user_id
    # (username is non-numeric and parse_user_id() would reject it, 403ing every
    # real user), X-Actor-Name=username for audit, same as the other patient
    # proxies above.
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get(
        "records",
        f"/patients/{patient_id}/reconciliation",
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
    # Stage 4 (Week 5, RIV-175): forward_status=True, same round-8 fix as
    # proxy_intake — the default silently flattened scheduling-service's
    # real 201 into a bare 200, which also would have masked a 422 (e.g. a
    # missing idempotency_key) as a false-looking 200 with an error body.
    return _post("scheduling", "/appointments", payload, forward_status=True)


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
