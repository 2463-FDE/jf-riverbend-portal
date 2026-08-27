"""
gateway — backend-for-frontend / API gateway.

The Next.js portal talks only to this service; it fans out to the internal
FastAPI services and owns login/sessions.

Inherited shortcoming, partially resolved: every route used to accept any
authenticated "staff" session regardless
of role. require_permission (below) now gates each proxied route on a real
permission from config/roles.yaml, and four least-privilege roles
(front_desk/clinician/roi_clerk/scheduler) exist and are enforced — but
every existing/seeded account is still on the deprecated `staff` role, which
keeps its original full permission set. Migrating real accounts onto a
specific role is a separate, explicit follow-up (config/roles.yaml's
comment) — this repo has no staff-directory/job-function data to do that
migration itself.

PR #23 and a later follow-up: sessions used to never expire; they now
carry both an idle Redis TTL (refreshed per request) and an absolute lifetime
cap enforced regardless of activity — see security.create_session/get_session.
auth.yaml is descriptive only, not read by this module.

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
import json
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

import patient_invitations
import roles_config
from config import settings
from db import get_db
from logging_config import configure
from models import InsuranceCoverage, Patient, PatientAccessGrant, PatientInvitation, User
from security import create_session, destroy_session, get_session, hash_password, verify_password
from visit_authorization import (
    find_authorized_appointment,
    has_active_grant,
    latest_insurance_coverage,
    parse_user_id,
)

log = configure(settings.service_name)

_MIN_INTERNAL_TOKEN_LENGTH = 32
# Patient-chosen, so it needs a floor. auth.yaml's password_min_length of 6 is
# the legacy staff value and is too low for a credential to a medical record.
_MIN_PATIENT_PASSWORD_LENGTH = 12  # matches intake-service/records-service's own floor


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
    # PR #26 review: config/roles.yaml is loaded lazily on the first
    # require_permission call, so a deployment that cannot see the file started
    # cleanly, passed /healthz and /login, and then 500'd every authenticated
    # route — the failure mode the old per-service Docker build context caused
    # (the file lives at the repo root, outside services/gateway/). Load it here
    # so a misconfigured deploy fails loudly at startup instead, matching how
    # the internal-token check above already behaves.
    try:
        roles_config.reload()
        if not roles_config.roles():
            raise ValueError("no roles defined")
    except Exception as e:
        raise RuntimeError(
            f"could not load the RBAC config from {roles_config.config_path()!r} "
            f"({type(e).__name__}: {e}) — refusing to start, because every "
            f"authorized route would fail. Check that config/roles.yaml is present "
            f"in the image (see services/gateway/Dockerfile) or set "
            f"ROLES_CONFIG_PATH to its location."
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


def require_permission(permission: str):
    """Per-route authorization on top of require_session's "is this any
    authenticated staff session" check.
    Every account maps to a role (config/roles.yaml); an authenticated
    session whose role lacks `permission` gets 403, not the route's data —
    same fail-closed posture as an unknown role (roles_config.permissions_for
    returns no permissions for a role that isn't defined at all).

    Returns a dependency, not a session directly, so it drops into a route
    signature exactly where `Depends(require_session)` used to be:
    `session: dict = Depends(require_permission("patients.write"))`.
    """

    def _check(session: dict = Depends(require_session)) -> dict:
        role = session.get("role")
        if permission not in roles_config.permissions_for(role):
            raise HTTPException(
                status_code=403, detail=f"role '{role}' lacks permission '{permission}'"
            )
        return session

    return _check


@app.get("/healthz")
def healthz():
    if not _internal_token_is_configured():
        raise HTTPException(status_code=503, detail="internal_service_token not configured")
    return {"status": "ok", "service": settings.service_name}


@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """
    Issue a session token. Password only — there is no second factor here.

    A working TOTP implementation was built and tested on this branch and
    then removed from it: the client's 2026-08-12 direction was to park MFA
    for a later, complete rollout (backup codes, supervisor-verified reset,
    reset logging, shared-login remediation, pilot clinic, grace period,
    dated cutover) rather than ship the mechanism alone this cycle. The
    prototype lives on `feat/mfa-totp-parked`, unmerged, with a planning
    card — see that branch's PR. Do not re-add a partial second factor here.

    Login rejects inactive users up front; the token carries the stable
    users.id and both an idle and an absolute Redis TTL (see
    create_session). Per-request is_active revalidation for chart data is
    enforced at records-service's SqlPatientAccessGate (PR #23 round 2).
    """
    try:
        user = db.execute(select(User).where(User.username == req.username)).scalar_one_or_none()
    except Exception as e:  # DB down in local dev without compose
        log.error("login db error: %s", e)
        raise HTTPException(status_code=503, detail="auth backend unavailable")

    # Password FIRST, status second. The combined check this replaces was
    # correct but could not carry the client's unmapped-account message without
    # turning it into an account-existence oracle: anyone could probe usernames
    # and learn which ones the roster does not cover, without knowing a
    # password. Verifying credentials before revealing status means the message
    # only ever reaches someone who already authenticated as that user.
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid username or password")

    if not user.is_active:
        # Branch 9 part 2. The roster migration disables accounts the client's
        # roster does not cover, and the client specified this copy verbatim.
        # Every occurrence is logged so the list can go back to them — that is
        # the deliverable, not a debugging aid.
        if user.disabled_reason and user.disabled_reason.startswith("role_migration_"):
            log.warning(
                "login denied: unmapped account user=%s reason=%s",
                user.username, user.disabled_reason,
            )
            raise HTTPException(
                status_code=403,
                detail="access is being updated, contact your supervisor",
            )
        # Any other inactive account keeps the original, deliberately
        # indistinguishable response.
        raise HTTPException(status_code=401, detail="invalid username or password")

    # P1 identity foundation (w8-planner-2): an ACTIVE account whose role is
    # not one config/roles.yaml actually defines must not receive a session
    # at all. The roster migration disables the specific accounts it knows
    # about via disabled_reason above, but that only covers accounts the
    # migration has already touched — this is the general invariant for any
    # account whose role has drifted, was mistyped directly in the database,
    # or predates a role being renamed/removed from the grid. permissions_for
    # already fails closed for an unmapped role on every protected route
    # (roles_config.py), so this makes the same fail-closed posture apply to
    # session issuance itself rather than only to what the session can later
    # do. Same generic, indistinguishable response as any other login
    # failure — a distinct message here would let a caller who already knows
    # a valid password learn that a role config problem exists.
    if not roles_config.roles().get(user.role):
        log.warning("login denied: role '%s' is not defined in roles.yaml user=%s", user.role, user.username)
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


# --------------------------------------------------------------------------- #
# patient portal invitations (S1)
#
# Authorization is NOT extended here. Activation creates a users row plus ONE
# patient_access_grants row for that patient's own chart, so the same gate that
# scopes staff scopes the patient — the client's point that this is one
# mechanism applied to a different principal, not two mechanisms.
# --------------------------------------------------------------------------- #
class IssueInvitationRequest(BaseModel):
    patient_id: int


def _revoke_lapsed_invitations(db: Session, patient_id: int) -> int:
    """Close out any invitation for this patient whose window has passed.

    This exists because `patient_invitations_one_live_per_patient` cannot see
    expiry: a partial-index predicate must be IMMUTABLE, so it cannot call
    now(). Without this, a lapsed code kept its slot in that index forever and
    the front desk could never issue the patient another one — they were told
    to "revoke it first" by a 409 about an invitation that had already expired.

    Marking the lapsed row revoked turns a state the index cannot express into
    one it can. It is not a weakening: the row was already unusable, since
    invitation_state() rejects an expired code at redemption. This only makes
    the database agree with what redemption already enforced.

    Deliberately not a background job — a sweep would leave the lockout in
    place until it next ran, and the only moment the answer matters is the
    moment someone tries to issue.
    """
    lapsed = (
        db.query(PatientInvitation)
        .filter(
            PatientInvitation.patient_id == patient_id,
            PatientInvitation.activated_at.is_(None),
            PatientInvitation.revoked_at.is_(None),
            PatientInvitation.expires_at <= func.now(),
        )
        .update({PatientInvitation.revoked_at: func.now()}, synchronize_session=False)
    )
    return int(lapsed or 0)


@app.post("/patients/{patient_id}/invitation", status_code=201)
def issue_patient_invitation(
    patient_id: int,
    session: dict = Depends(require_permission("patients.write")),
    db: Session = Depends(get_db),
):
    """Front desk issues a portal invitation while registering the patient.

    Gated on patients.write — the permission the roles that register patients
    already hold, and the same desk that verifies identity in person. Deliberately
    not a new permission: inventing one would mean nobody holds it until the
    grid is amended.

    patients.write alone says nothing about THIS patient — it is a role-wide
    permission, not a per-chart one. Everything else this route reads (existing
    account, existing invitation) is scoped by patient_id, so authorization has
    to be too: the issuer must additionally hold an active grant for this
    specific chart, the same fact that gates reading it. Reused rather than a
    new check invented for this route: `has_active_grant` is the identical
    active-grant/active-user query `find_authorized_appointment` already runs.

    The code is returned exactly once, in this response, and never stored. If it
    is lost the invitation is revoked and reissued; there is no path that reads
    an existing code back, because none should exist.
    """
    issuer_id = parse_user_id(session.get("user_id"))
    if issuer_id is None:
        raise HTTPException(status_code=403, detail="not authorized")

    # Fail closed before anything else: a patient_id that resolves to nothing
    # is not a lookup failure the caller should be able to turn into an
    # invitation. Checked before the grant query below for the same reason
    # `_authorize_or_deny` checks a grant before the patient's own row exists
    # to be read — a nonexistent patient and an ungranted one both refuse the
    # same way, so this route is not an existence oracle either.
    if db.execute(select(Patient.id).where(Patient.id == patient_id)).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="no such patient")

    if not has_active_grant(db, user_id=issuer_id, patient_id=patient_id):
        raise HTTPException(status_code=403, detail="not authorized")

    # An account already exists for this chart (users.patient_id is unique —
    # migration 017, `users_patient_id_unique`) — issuing a second invitation
    # would let a second password be set on the same chart via a different
    # invitation later, or simply confuse "invited" with "already has access".
    # One portal account per chart is the invariant; this is where it is
    # enforced on the issuing side (activation enforces it from the other side
    # via that same unique index). Deliberately NOT filtered to is_active: the
    # unique constraint binds the column regardless of the row's active state,
    # so a disabled account still occupies the slot — issuing against it would
    # mint a code whose activation then dies on the same unique index, leaving
    # the patient with a code that can never be redeemed (round-1 review, M1).
    existing_account = db.execute(
        select(User.id).where(User.patient_id == patient_id)
    ).scalar_one_or_none()
    if existing_account is not None:
        # Structured, not a string the frontend has to parse: this and the
        # LIVE_INVITATION conflict below are different states requiring
        # different UI (no revoke control makes sense for an account that
        # already exists), and a caller distinguishing them by matching
        # English text is exactly the coupling a machine-readable reason
        # exists to avoid.
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "ACTIVE_PORTAL_ACCOUNT",
                "message": "this patient already has an active portal account",
            },
        )

    code = patient_invitations.generate_code()
    invitation = PatientInvitation(
        patient_id=patient_id,
        code_hash=patient_invitations.hash_code(code),
        issued_by=issuer_id,
        expires_at=patient_invitations.default_expiry(),
    )
    try:
        # One transaction: a lapsed code is closed out and the replacement
        # inserted together, so no window exists where the patient has neither.
        lapsed = _revoke_lapsed_invitations(db, patient_id)
        db.add(invitation)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        # Expired rows were just cleared, so a conflict here means a genuinely
        # live code is outstanding — the one case where refusing is right. Do
        # not echo the database error: it names constraints and columns.
        log.warning("invitation issue rejected for patient_id=%s", patient_id)
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "LIVE_INVITATION",
                "message": (
                    "this patient already has an unexpired invitation; revoke it "
                    "before issuing another"
                ),
            },
        )

    if lapsed:
        log.info(
            "revoked %s lapsed invitation(s) before reissue patient_id=%s", lapsed, patient_id
        )

    log.info("portal invitation issued patient_id=%s by user_id=%s", patient_id, issuer_id)
    return {
        "patient_id": patient_id,
        "code": code,  # shown once, never stored, never retrievable
        "expires_at": invitation.expires_at.isoformat() if invitation.expires_at else None,
    }


@app.delete("/patients/{patient_id}/invitation")
def revoke_patient_invitation(
    patient_id: int,
    session: dict = Depends(require_permission("patients.write")),
    db: Session = Depends(get_db),
):
    """Kill an outstanding invitation before anyone redeems it.

    Needed for the case the issue route's 409 points at, and for the ordinary
    front-desk mistake: a code read aloud to the wrong person, or issued
    against the wrong patient. Until this existed the only answer was a code
    that stayed valid for fourteen days with no way to close it.

    Idempotent, and deliberately does not report whether a code was actually
    outstanding — same permission AND grant as issuing (2026-08-22 — this had
    lagged issuance's own has_active_grant requirement), so revoking leaks
    nothing beyond what issuing already could, and a caller who cannot issue
    for this chart cannot revoke for it either.
    """
    revoker_id = parse_user_id(session.get("user_id"))
    if revoker_id is None:
        raise HTTPException(status_code=403, detail="not authorized")
    if not has_active_grant(db, user_id=revoker_id, patient_id=patient_id):
        raise HTTPException(status_code=403, detail="not authorized")

    revoked = (
        db.query(PatientInvitation)
        .filter(
            PatientInvitation.patient_id == patient_id,
            PatientInvitation.activated_at.is_(None),
            PatientInvitation.revoked_at.is_(None),
        )
        .update({PatientInvitation.revoked_at: func.now()}, synchronize_session=False)
    )
    db.commit()

    log.info(
        "portal invitation revoked patient_id=%s count=%s by user_id=%s",
        patient_id,
        revoked,
        revoker_id,
    )
    # Already-activated invitations are untouched: revoking an invitation must
    # not silently disable a working account. Closing an active account is a
    # separate action on the account itself.
    return {"patient_id": patient_id, "revoked": int(revoked or 0)}


class ActivateRequest(BaseModel):
    code: str
    password: str


@app.post("/patient/activate")
def activate_patient_account(req: ActivateRequest, db: Session = Depends(get_db)):
    """Redeem an invitation and set a password. Public by necessity — the
    person redeeming has no account yet.

    Every failure returns the SAME response. Distinguishing "expired" from
    "unknown" would confirm that a code existed, turning this endpoint into an
    oracle for guessing them. The specific reason is logged, never returned.
    """
    generic = HTTPException(status_code=400, detail="invalid or expired invitation code")

    if len(req.password or "") < _MIN_PATIENT_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {_MIN_PATIENT_PASSWORD_LENGTH} characters",
        )

    try:
        invitation = db.execute(
            select(PatientInvitation).where(
                PatientInvitation.code_hash == patient_invitations.hash_code(req.code)
            )
        ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("activation: invitation lookup failed")
        raise HTTPException(status_code=503, detail="could not activate right now; please retry")

    reason = patient_invitations.invitation_state(invitation)
    if reason is not None:
        log.info("activation refused reason=%s", reason)
        raise generic
    if not patient_invitations.codes_match(req.code, invitation.code_hash):
        # Belt and braces: the lookup already matched on hash, but comparing in
        # constant time keeps a timing signal out of the path regardless.
        raise generic

    try:
        account = _activate_invitation(db, invitation, req.password)
    except SQLAlchemyError:
        db.rollback()
        log.exception("activation: could not create the account")
        raise HTTPException(status_code=503, detail="could not activate right now; please retry")

    log.info("patient account activated user_id=%s patient_id=%s", account.id, invitation.patient_id)
    return {"status": "activated", "username": account.username}


def _activate_invitation(db: Session, invitation, password: str) -> User:
    """Claim the invitation, create the account, and grant it exactly one chart.

    The claim is written first and the row is only claimed if it is still
    unclaimed, so two simultaneous redemptions of one code cannot both proceed —
    the loser sees zero rows updated and is rejected. All of it commits as one
    transaction: an account without its grant would be a patient who can sign in
    and see nothing, and a grant without an account is an orphan.

    The patient row is loaded HERE, inside this same transaction, and is what
    `full_name` is set from. It used to be left `None` — nothing populated it,
    ever, so every activated account showed no name anywhere the portal
    displays one, indistinguishable from a real lookup failure. `patients` is
    the authoritative source of a patient's name (records-service's table,
    mirrored read-only above as `Patient`); an invitation whose patient_id no
    longer resolves there is claimed and useless, so this fails the same
    generic way every other activation failure does — an invitation is not an
    oracle for whether a patient_id exists.
    """
    claimed = db.execute(
        update(PatientInvitation)
        .where(
            PatientInvitation.id == invitation.id,
            PatientInvitation.activated_at.is_(None),
            PatientInvitation.revoked_at.is_(None),
        )
        .values(activated_at=func.now())
        .returning(PatientInvitation.id)
    ).scalar_one_or_none()
    if claimed is None:
        db.rollback()
        raise HTTPException(status_code=400, detail="invalid or expired invitation code")

    patient = db.execute(
        select(Patient).where(Patient.id == invitation.patient_id)
    ).scalar_one_or_none()
    if patient is None:
        db.rollback()
        raise HTTPException(status_code=400, detail="invalid or expired invitation code")

    account = User(
        username=patient_invitations.username_for_patient(invitation.patient_id),
        password_hash=hash_password(password),
        full_name=patient.name,
        role="patient",
        patient_id=invitation.patient_id,
        is_active=True,
    )
    db.add(account)
    db.flush()

    # THE grant. One row, for their own chart, in the same table staff use —
    # which is what makes every existing scoping query work unchanged.
    #
    # Through the ORM rather than raw SQL: the model is right there, and an
    # INSERT ... ON CONFLICT would have pinned this to Postgres syntax for a
    # conflict that cannot arise — the account is created two statements above,
    # so it cannot already hold a grant.
    db.add(PatientAccessGrant(user_id=account.id, patient_id=invitation.patient_id))
    db.execute(
        update(PatientInvitation)
        .where(PatientInvitation.id == invitation.id)
        .values(activated_user_id=account.id)
    )
    db.commit()
    return account


@app.post("/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    """End the session server-side and say so definitively.

    Shared-workstation fix: the frontend used to swallow any failure here and
    clear its own storage anyway, so a logout that never reached Redis showed
    the user a signed-out screen while their session stayed valid — on a
    machine the next person was about to use. This route now reports the
    outcome, and a Redis failure surfaces as 503 instead of a cheerful "ok",
    so the caller can keep the user on the page and retry rather than
    pretending.
    """
    token = _bearer(authorization)
    if not token:
        # Nothing to end. Not an error — a caller with no token is already
        # in the state logout is meant to produce.
        return {"status": "ok", "session_ended": False}
    try:
        destroy_session(token)
    except Exception as e:
        log.error("logout failed to reach the session store (error_type=%s)", type(e).__name__)
        raise HTTPException(status_code=503, detail="could not end the session; please retry")
    return {"status": "ok", "session_ended": True}


@app.get("/me")
def me(session: dict = Depends(require_session)):
    return {"username": session.get("username"), "role": session.get("role")}


# --------------------------------------------------------------------------- #
# intake / eligibility
# --------------------------------------------------------------------------- #
@app.post("/intake")
def proxy_intake(payload: dict, session: dict = Depends(require_permission("patients.write"))):
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


@app.post("/intake/instructions")
def proxy_intake_instructions(payload: dict, session: dict = Depends(require_session)):
    """Stage 1 (feature-readiness): patient-friendly intake-step explanation.
    Same X-Internal-Token transport-trust pattern as proxy_intake — proves
    the call came through the gateway's own require_session check, not a
    direct caller hitting intake-service's published host port. No patient
    data crosses this path; the only body field is which of the four known
    wizard steps the caller is on (intake-service validates it against a
    closed set and rejects anything else)."""
    headers = {"X-Internal-Token": settings.internal_service_token}
    return _post("intake", "/intake/instructions", payload, headers=headers, forward_status=True)


@app.get("/eligibility")
def proxy_eligibility(insurance_id: str, session: dict = Depends(require_permission("billing.read"))):
    return _get("eligibility", "/eligibility", params={"insurance_id": insurance_id})


# --------------------------------------------------------------------------- #
# Stage 3: async eligibility job status/retry + visit-scoped assistant turns
#
# proxy_eligibility_job_status/proxy_eligibility_job_retry below now require
# require_permission("billing.read"), not just any authenticated session —
# but that is PER-ACTION authorization
# (does this role do billing lookups at all), not PER-RESOURCE authorization:
# neither route checks that job_id actually belongs to a patient/visit this
# caller is authorized for. Any billing.read-permitted caller can still read
# or retry ANY job_id — the same category of gap as RIV-201 (see the IDOR
# note on proxy_records above), just not the one item 4 closes. Documented,
# existing debt, not something this stage widens or attempts to fix.
#
# proxy_visit_message below is the one exception (Stage 2, feature-
# readiness): visit_id IS now checked against the caller's authorized
# patients — see visit_authorization.py.
# --------------------------------------------------------------------------- #
@app.get("/eligibility/jobs/{job_id}")
def proxy_eligibility_job_status(job_id: str, session: dict = Depends(require_permission("billing.read"))):
    return _get(
        "eligibility", f"/eligibility/jobs/{job_id}", headers=_correlation_headers(), forward_status=True
    )


@app.post("/eligibility/jobs/{job_id}/retry")
def proxy_eligibility_job_retry(job_id: str, session: dict = Depends(require_permission("billing.read"))):
    return _post(
        "eligibility", f"/eligibility/jobs/{job_id}/retry", {}, headers=_correlation_headers(), forward_status=True
    )


# --------------------------------------------------------------------------- #
# W9.3 — Coverage & Eligibility workspace.
#
# The two routes above (proxy_eligibility_job_status/retry) are exactly the
# "arbitrary job id is not an authorization boundary" gap their own comment
# documents — this workspace does not use them. Every route below is scoped
# by patient_id AND coverage_id, checks an active patient_access_grants row
# the same way proxy_visit_message does (has_active_grant, not merely the
# role permission), verifies the coverage belongs to the named patient
# BEFORE touching eligibility-service, and never returns a raw job_id to the
# browser — insurance_coverages.verification_job_id (migration 023) is the
# only place a job id is held, read and written server-side only.
# --------------------------------------------------------------------------- #

def _mask_member_id(member_id: Optional[str]) -> Optional[str]:
    """All but the last 4 characters. A masked id is still enough for staff
    to confirm they picked the right coverage without displaying the whole
    identifier on a screen that is not the record of it."""
    if not member_id:
        return None
    tail = member_id[-4:]
    return f"{'*' * max(len(member_id) - 4, 0)}{tail}"


def _coverage_out(c: InsuranceCoverage) -> dict:
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "payer_name": c.payer_name,
        "plan_type": c.plan_type,
        "group_number": c.group_number,
        "member_id_masked": _mask_member_id(c.member_id),
        "status": c.status,
        "verified_at": c.verified_at.isoformat() if c.verified_at else None,
        "has_member_id": bool(c.member_id),
    }


def _ok_body(result) -> Optional[dict]:
    """The parsed JSON body from a forward_status=True call, only when it
    actually succeeded (200) — None for any other status or a network
    failure, so callers branch on "did I get a usable body" once instead of
    re-deriving it from a JSONResponse's status_code at each call site."""
    if isinstance(result, JSONResponse):
        if result.status_code != 200:
            return None
        return json.loads(result.body) if result.body else {}
    return result if isinstance(result, dict) else None


def _authorized_coverage(
    db: Session, *, patient_id: int, coverage_id: int, for_update: bool = False
) -> InsuranceCoverage:
    """The coverage, or a 404 — identically, whether the id does not exist or
    belongs to a different patient. Never distinguish the two: a coverage id
    is as guessable/sequential as any other integer primary key here.

    `for_update` (round-1 review, 2026-08-23): verify and the status check
    both read `verification_job_id`, decide something from it, and write it
    or the coverage's status back — without a lock, two concurrent calls can
    both read the same "nothing in flight" state and each create its own
    live payer job for the same coverage. SELECT ... FOR UPDATE serializes
    them on this row for the rest of the transaction, so the second caller's
    read reflects the first caller's write. (SQLite, used by this file's
    tests, has no row-level lock and silently ignores the clause — the
    tests exercise the mapping logic, not the concurrency guarantee itself,
    which only a real concurrent-request test against Postgres could.)
    """
    query = select(InsuranceCoverage).where(
        InsuranceCoverage.id == coverage_id, InsuranceCoverage.patient_id == patient_id
    )
    if for_update:
        query = query.with_for_update()
    coverage = db.execute(query).scalar_one_or_none()
    if coverage is None:
        raise HTTPException(status_code=404, detail="coverage not found")
    return coverage


def _require_patient_grant(db: Session, session: dict, patient_id: int) -> int:
    actor_id = parse_user_id(session.get("user_id"))
    if actor_id is None:
        raise HTTPException(status_code=403, detail="not authorized")
    if not has_active_grant(db, user_id=actor_id, patient_id=patient_id):
        raise HTTPException(status_code=403, detail="not authorized")
    return actor_id


@app.get("/patients/{patient_id}/coverages")
def list_patient_coverages(
    patient_id: int,
    session: dict = Depends(require_permission("billing.read")),
    db: Session = Depends(get_db),
):
    _require_patient_grant(db, session, patient_id)
    rows = db.execute(
        select(InsuranceCoverage).where(InsuranceCoverage.patient_id == patient_id).order_by(InsuranceCoverage.id)
    ).scalars().all()
    return {"items": [_coverage_out(c) for c in rows]}


# Job status -> a safe display category. Never the raw exception type name
# (see EligibilityJobResponse.error's own "PHI-safe" comment upstream) —
# mapped into the SAME small vocabulary a "succeeded" result's result_status
# already uses, so the frontend has one status field to branch on regardless
# of which stage of the job produced it.
def _job_category(job_status: str, result_status: Optional[str], manual_retry_count: int, max_manual_retries: int) -> dict:
    if job_status in ("queued", "running"):
        return {"category": "pending", "can_retry": False}
    if job_status == "succeeded":
        return {"category": result_status or "unknown", "can_retry": False}
    if job_status in ("failed", "retryable"):
        return {"category": "unavailable", "can_retry": manual_retry_count < max_manual_retries}
    if job_status == "dead_letter":
        return {"category": "unavailable", "can_retry": manual_retry_count < max_manual_retries}
    return {"category": "unknown", "can_retry": False}


@app.post("/patients/{patient_id}/coverages/{coverage_id}/verify", status_code=201)
def verify_patient_coverage(
    patient_id: int,
    coverage_id: int,
    session: dict = Depends(require_permission("billing.write")),
    db: Session = Depends(get_db),
):
    """Request a verification. Derives the member id server-side from the
    coverage row the path already named and authorized — the browser never
    supplies or sees it in full (see _mask_member_id above).

    for_update=True (round-1 review): without a lock, two near-simultaneous
    Verify clicks both read no-job-in-flight and both create a live payer
    job for the same coverage. Held through this whole function, including
    the reuse-check GET to eligibility-service — the second caller blocks
    until the first's transaction ends and then reads the id it just wrote."""
    _require_patient_grant(db, session, patient_id)
    coverage = _authorized_coverage(db, patient_id=patient_id, coverage_id=coverage_id, for_update=True)
    if not coverage.member_id:
        raise HTTPException(status_code=400, detail="this coverage has no member id on file")

    if not settings.payer_api_key:
        # No real payer key exists in this training environment (never will,
        # per ADR/README) — make NO outbound call at all rather than run the
        # real check pipeline with nothing behind it. This is the entire
        # simulation boundary: one branch, taken before eligibility-service
        # is ever contacted, not a flag threaded through it.
        return {"category": "simulated", "message": "Synthetic training — no payer contacted", "can_retry": False}

    headers = _correlation_headers()
    headers["X-Internal-Token"] = settings.internal_service_token

    # Idempotent: reuse a job already in flight for this coverage rather
    # than starting a second live check behind it. Only QUEUED/RUNNING
    # counts as "in flight" — a terminal job (succeeded, failed, dead_letter)
    # means this Verify click is a genuinely new request, not a duplicate.
    if coverage.verification_job_id:
        existing = _get(
            "eligibility", f"/eligibility/jobs/{coverage.verification_job_id}",
            headers=headers, forward_status=True,
        )
        existing_body = _ok_body(existing)
        if existing_body and existing_body.get("status") in ("queued", "running"):
            return _job_category(
                existing_body.get("status", ""), existing_body.get("result_status"),
                existing_body.get("manual_retry_count", 0), existing_body.get("max_manual_retries", 0),
            )

    result = _post(
        "eligibility", "/eligibility/jobs",
        {"insurance_id": coverage.member_id, "idempotency_key": f"coverage:{coverage_id}:{uuid.uuid4().hex}"},
        headers=headers,
    )
    if not isinstance(result, dict) or "job_id" not in result:
        raise HTTPException(status_code=503, detail="could not start a verification right now")

    coverage.verification_job_id = result["job_id"]
    db.commit()
    mapped = _job_category(result.get("status", ""), result.get("result_status"),
                            result.get("manual_retry_count", 0), result.get("max_manual_retries", 0))
    return mapped


@app.get("/patients/{patient_id}/coverages/{coverage_id}/eligibility-status")
def get_coverage_eligibility_status(
    patient_id: int,
    coverage_id: int,
    session: dict = Depends(require_permission("billing.read")),
    db: Session = Depends(get_db),
):
    # for_update=True (round-1 review): this route can also durably write
    # coverage.status/verified_at below on a terminal result — the same
    # concurrent-write hazard verify has, held the same way.
    _require_patient_grant(db, session, patient_id)
    coverage = _authorized_coverage(db, patient_id=patient_id, coverage_id=coverage_id, for_update=True)
    if not coverage.verification_job_id:
        return {"category": "unknown", "message": "Not yet verified", "can_retry": False}

    headers = _correlation_headers()
    headers["X-Internal-Token"] = settings.internal_service_token
    result = _get(
        "eligibility", f"/eligibility/jobs/{coverage.verification_job_id}", headers=headers, forward_status=True
    )
    body = _ok_body(result)
    if body is None:
        if isinstance(result, JSONResponse) and result.status_code == 404:
            # The job record fell out of Redis (its TTL is bounded — see
            # jobs.py) — not an error, just nothing left to report. A fresh
            # Verify starts a new one.
            return {"category": "unknown", "message": "Not yet verified", "can_retry": False}
        raise HTTPException(status_code=503, detail="could not check verification status right now")

    mapped = _job_category(body.get("status", ""), body.get("result_status"),
                            body.get("manual_retry_count", 0), body.get("max_manual_retries", 0))
    if mapped["category"] in ("active", "inactive", "stale", "unknown") and body.get("status") == "succeeded":
        # A terminal, checked result is what makes the durable record
        # trustworthy going forward — the same "outcome truthfulness" the
        # reference asks for, not merely a transient poll response.
        coverage.status = mapped["category"]
        # func.now() — the moment THIS check recorded the result, not a
        # re-parse of the job's own ISO timestamp string (which would need
        # parsing to be a real datetime for a timestamptz column anyway).
        coverage.verified_at = func.now()
        db.commit()
    return mapped


@app.post("/patients/{patient_id}/coverages/{coverage_id}/eligibility-retry")
def retry_coverage_eligibility(
    patient_id: int,
    coverage_id: int,
    session: dict = Depends(require_permission("billing.write")),
    db: Session = Depends(get_db),
):
    _require_patient_grant(db, session, patient_id)
    coverage = _authorized_coverage(db, patient_id=patient_id, coverage_id=coverage_id)
    if not coverage.verification_job_id:
        raise HTTPException(status_code=409, detail="nothing to retry — request a verification first")

    headers = _correlation_headers()
    headers["X-Internal-Token"] = settings.internal_service_token
    result = _post(
        "eligibility", f"/eligibility/jobs/{coverage.verification_job_id}/retry", {},
        headers=headers, forward_status=True,
    )
    body = _ok_body(result)
    if body is None and isinstance(result, JSONResponse) and result.status_code == 409:
        # Current job state is not eligible for a manual retry right now
        # (still in flight, already succeeded, or retries exhausted) —
        # eligibility-service's own body IS the current job even on 409, so
        # map it the same way a status check would rather than surfacing a
        # bare error.
        body = json.loads(result.body) if result.body else {}
    if body is None:
        raise HTTPException(status_code=503, detail="could not retry this verification right now")

    return _job_category(body.get("status", ""), body.get("result_status"),
                          body.get("manual_retry_count", 0), body.get("max_manual_retries", 0))


@app.post("/visits/{visit_id}/messages")
def proxy_visit_message(
    visit_id: str,
    payload: dict,
    session: dict = Depends(require_permission("patients.read")),
    db: Session = Depends(get_db),
):
    """Stage 2 (feature-readiness): unlike every route above, this one is no
    longer a bare forward — `visit_id` is required to be a real
    `appointments.id`, and the caller must hold an active
    `patient_access_grants` row for that appointment's patient (see
    visit_authorization.py). This closes the gap the module comment above
    used to document as "never checked against the specific ... visit_id
    being requested": a session alone no longer scopes this route to every
    patient in the system.

    `patient_id`/`insurance_id`/`coverage_on_file` are NEVER taken from
    `payload` — whatever a caller sends there is dropped. They're derived
    here, server-side, from the authorized appointment and the patient's
    insurance on file, which is the only way eligibility-service's
    `bind_visit_context` ever sees them (mirrors the same "server-derived,
    not client-supplied" principle proxy_intake already applies to
    X-Actor-Id). `coverage_on_file` (w-9-2-planner P1a) is the stored
    payer/plan/status snapshot get_coverage_on_file answers from without
    ever contacting the payer — see visit_authorization.py's
    latest_insurance_coverage.
    """
    downstream_payload = _authorize_visit_and_build_payload(visit_id, payload, session, db)
    return _post(
        "eligibility",
        f"/visits/{visit_id}/messages",
        downstream_payload,
        headers=_correlation_headers(),
        forward_status=True,
    )


@app.post("/visits/{visit_id}/messages/stream")
def proxy_visit_message_stream(
    visit_id: str,
    payload: dict,
    session: dict = Depends(require_permission("patients.read")),
    db: Session = Depends(get_db),
):
    """w-9-2-planner P1b: streaming counterpart to proxy_visit_message.

    Authorization is IDENTICAL and happens ENTIRELY before any downstream
    call opens — _authorize_visit_and_build_payload below is the exact
    same function proxy_visit_message calls, so there is no second,
    separately-maintained copy of the grant check to drift out of sync.
    Only once that returns successfully does this relay a byte stream from
    eligibility-service's own streaming endpoint straight through; nothing
    about the stream's CONTENTS is inspected or altered here — the
    sanitization boundary (never a prompt, tool payload, retrieved text, or
    raw error) is enforced upstream, in libs/eligibility_agent and
    agent_wiring.py, not re-implemented at this layer.

    w-9-2-planner P1b review fix (STREAM-UPSTREAM-STATUS): a
    StreamingResponse commits to HTTP 200 the moment it's constructed and
    returned — well before its generator body ever runs — so a naive
    `with httpx.stream(...) as upstream:` INSIDE the generator can only ever
    surface a non-2xx upstream status (401/422/503) as a 200 stream carrying
    a sanitized error line. That is inconsistent with proxy_visit_message's
    own forward_status=True, and hides the real status from any caller that
    checks it. The connection is opened here, BEFORE returning any response,
    with the same status forwarded verbatim (mirrors _post's forward_status
    shape) for a non-2xx; only once a 2xx is confirmed does this method
    switch into streaming, reusing that same already-open connection rather
    than opening a second one.
    """
    downstream_payload = _authorize_visit_and_build_payload(visit_id, payload, session, db)

    client = httpx.Client(timeout=60)
    try:
        upstream = client.send(
            client.build_request(
                "POST",
                f"{SERVICES['eligibility']}/visits/{visit_id}/messages/stream",
                json=downstream_payload,
                headers=_internal_headers(_correlation_headers()),
            ),
            stream=True,
        )
    except Exception as e:
        client.close()
        log.error("proxy stream %s failed to open: %s", visit_id, e)
        return JSONResponse(status_code=502, content={"error": str(e)})

    if upstream.status_code >= 300:
        body = _safe_json(upstream)
        upstream.close()
        client.close()
        return JSONResponse(status_code=upstream.status_code, content=body)

    def relay():
        try:
            for chunk in upstream.iter_bytes():
                yield chunk
        except Exception as e:
            # Unlike the pre-stream-open failure above, the 200 status is
            # already committed by this point — a mid-stream outage must
            # still end the stream with one sanitized terminal event, not a
            # silently truncated response the browser reads as complete.
            log.error("proxy stream %s failed mid-stream: %s", visit_id, e)
            yield (
                json.dumps(
                    {
                        "kind": "error",
                        "text": "The eligibility assistant isn't available right now. Please check eligibility manually.",
                        "tool_called": None,
                        "eligibility_status": None,
                        "termination_reason": "provider_error",
                        "turns_used": None,
                    }
                )
                + "\n"
            ).encode("utf-8")
        finally:
            upstream.close()
            client.close()

    return StreamingResponse(relay(), media_type="application/x-ndjson")


def _authorize_visit_and_build_payload(visit_id: str, payload: dict, session: dict, db: Session) -> dict:
    """Shared authorization + payload-derivation for both the blocking and
    streaming visit-message routes — see proxy_visit_message's own
    docstring for the full rationale (visit_id must resolve to a real,
    grant-authorized appointment; patient_id/insurance_id/coverage_on_file
    are always server-derived, never taken from `payload`). Raises
    HTTPException on any authorization/lookup failure; the caller does not
    need its own try/except around this call."""
    try:
        appointment_id = int(visit_id)
    except (TypeError, ValueError):
        # Same response as a real-but-unauthorized id — never reveal whether
        # the string was even shaped like a valid one (no format oracle).
        raise HTTPException(status_code=403, detail="not authorized for this visit")

    user_id = parse_user_id(session.get("user_id"))
    if user_id is None:
        raise HTTPException(status_code=403, detail="not authorized for this visit")

    try:
        appointment = find_authorized_appointment(db, user_id=user_id, appointment_id=appointment_id)
    except SQLAlchemyError as e:
        log.error("visit message: grant lookup failed (error_type=%s)", type(e).__name__)
        raise HTTPException(status_code=503, detail="authorization store unavailable")

    if appointment is None:
        raise HTTPException(status_code=403, detail="not authorized for this visit")

    message = payload.get("message") if isinstance(payload, dict) else None
    try:
        coverage = latest_insurance_coverage(db, patient_id=appointment.patient_id)
    except SQLAlchemyError as e:
        log.error("visit message: insurance lookup failed (error_type=%s)", type(e).__name__)
        raise HTTPException(status_code=503, detail="patient store unavailable")

    # w-9-2-planner P1a review fix (B1-row-mismatch): insurance_id and
    # coverage_on_file must describe the SAME coverage row. A separate
    # latest_insurance_member_id() query can select a different (older) row
    # than latest_insurance_coverage() whenever the newest row lacks a
    # member_id — that mismatch could hand verify_current_eligibility an
    # identifier for coverage other than the one shown as "on file". If the
    # single most-recent row has no member_id, insurance_id is None (live
    # verification declines) rather than falling back to a different row.
    insurance_id = coverage.member_id if coverage is not None else None

    coverage_on_file = (
        {
            "payer_name": coverage.payer_name,
            "plan_type": coverage.plan_type,
            "member_id_masked": _mask_member_id(coverage.member_id),
            "status": coverage.status,
            "verified_at": coverage.verified_at.isoformat() if coverage.verified_at else None,
        }
        if coverage is not None
        else None
    )

    return {
        "message": message,
        "patient_id": appointment.patient_id,
        "insurance_id": insurance_id,
        "coverage_on_file": coverage_on_file,
    }


# --------------------------------------------------------------------------- #
# patients / records
# --------------------------------------------------------------------------- #
@app.get("/patients")
def proxy_patients(
    session: dict = Depends(require_permission("patients.read")),
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
def proxy_patient(patient_id: int, session: dict = Depends(require_permission("patients.read"))):
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get("records", f"/patients/{patient_id}", headers=headers, forward_status=True)


@app.get("/patients/{patient_id}/records")
def proxy_records(patient_id: int, session: dict = Depends(require_permission("records.read"))):
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get("records", f"/patients/{patient_id}/records", headers=headers, forward_status=True)


@app.get("/records/search")
def proxy_search(q: str, session: dict = Depends(require_permission("records.read"))):
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
    session: dict = Depends(require_permission("records.read")),
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
    session: dict = Depends(require_permission("records.read")),
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
    # PR #26 review: this was the one proxied route left on require_session
    # alone, which made the authorization model inconsistent across scheduling.
    # Gated on appointments.write because that is the permission the roles who
    # need availability already hold; reading availability arguably deserves a
    # narrower appointments.read, which the fuller role model introduces — worth
    # refining then rather than inventing a permission no role has yet.
    session: dict = Depends(require_permission("appointments.write")),
    provider_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
):
    return _get("scheduling", "/slots", params={"provider_id": provider_id, "limit": limit})


@app.get("/appointments")
def proxy_list_appointments(
    patient_id: int,
    # PR #31 review [high]: this was gated on patients.read, but the signed
    # matrix makes the two distinct — roi_clerk and lab hold patients.read and
    # NOT appointments.read, so they could list any patient's appointments,
    # which the grid says is None for them. Reading appointments needs the
    # appointments permission.
    session: dict = Depends(require_permission("appointments.read")),
    db: Session = Depends(get_db),
):
    # w9-fixes P0 4.5: appointments.read is per-ACTION authorization (does
    # this role do appointment lookups at all), not per-RESOURCE — it says
    # nothing about THIS patient. Scheduling-service has no actor context of
    # its own to check that, so the grant boundary every other chart-adjacent
    # route already enforces (records, agent-draft, messaging, coverage) has
    # to be enforced here, the same way visit-chat already does for
    # appointment-scoped access.
    _require_patient_grant(db, session, patient_id)
    return _get("scheduling", "/appointments", params={"patient_id": patient_id})


@app.post("/appointments")
def proxy_book(payload: dict, session: dict = Depends(require_permission("appointments.write")), db: Session = Depends(get_db)):
    # w9-fixes P0 4.5: the browser names the patient_id it wants to book
    # for — checked against the caller's own grants before this reaches
    # scheduling-service, same reasoning as the GET above.
    patient_id = payload.get("patient_id") if isinstance(payload, dict) else None
    try:
        patient_id = int(patient_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="patient_id is required")
    _require_patient_grant(db, session, patient_id)
    # Stage 4 (Week 5, RIV-175): forward_status=True, same round-8 fix as
    # proxy_intake — the default silently flattened scheduling-service's
    # real 201 into a bare 200, which also would have masked a 422 (e.g. a
    # missing idempotency_key) as a false-looking 200 with an error body.
    return _post("scheduling", "/appointments", payload, forward_status=True)


@app.post("/appointments/{appointment_id}/cancel")
def proxy_cancel(
    appointment_id: int,
    session: dict = Depends(require_permission("appointments.write")),
    db: Session = Depends(get_db),
):
    # w9-fixes P0 4.5: cancellation names only an appointment_id — resolved
    # to its patient server-side via the SAME authorized-appointment query
    # visit-chat already uses, rather than trusting scheduling-service (which
    # has no actor context) to have checked anything. No existence oracle: a
    # nonexistent id and an ungranted one both 403 identically.
    actor_id = parse_user_id(session.get("user_id"))
    if actor_id is None:
        raise HTTPException(status_code=403, detail="not authorized")
    try:
        appointment = find_authorized_appointment(db, user_id=actor_id, appointment_id=appointment_id)
    except SQLAlchemyError:
        log.exception("cancel: grant lookup failed appointment_id=%s", appointment_id)
        raise HTTPException(status_code=503, detail="authorization store unavailable")
    if appointment is None:
        raise HTTPException(status_code=403, detail="not authorized")
    # Round-1 review: without forward_status, _post flattens ANY
    # scheduling-service response into a 200 (a 404 for an already-deleted
    # appointment, a 503 outage) — the browser's `r.ok` check would then read
    # "Appointment cancelled." for a cancel that never happened, with the
    # appointment still confirmed and its slot still booked. Same reasoning
    # as proxy_book's own forward_status=True below.
    return _post("scheduling", f"/appointments/{appointment_id}/cancel", {}, forward_status=True)


# --------------------------------------------------------------------------- #
# release of information
# --------------------------------------------------------------------------- #
@app.get("/roi/requests")
def proxy_roi_list(session: dict = Depends(require_permission("disclosures.read")), patient_id: Optional[int] = None):
    return _get("roi", "/roi/requests", params={"patient_id": patient_id})


@app.post("/roi/requests")
def proxy_roi_create(payload: dict, session: dict = Depends(require_permission("roi.write"))):
    return _post("roi", "/roi/requests", payload)


@app.post("/roi/requests/{request_id}/fulfill")
def proxy_roi_fulfill(
    request_id: int, payload: dict, session: dict = Depends(require_permission("roi.write"))
):
    # w8-planner-2: `payload` used to be hardcoded to {} here, discarding
    # whatever the caller sent — roi-service now REQUIRES an authorization
    # payload (reference/signed_at/signed_by) to fulfill at all, so it has
    # to actually reach the service.
    return _post("roi", f"/roi/requests/{request_id}/fulfill", payload)


@app.get("/roi/patients/{patient_id}/accounting")
def proxy_roi_accounting(
    patient_id: int, session: dict = Depends(require_permission("disclosures.read"))
):
    # 45 CFR 164.528 accounting of disclosures (w8-planner-2) — same
    # disclosures.read permission proxy_roi_list already requires; no new
    # RBAC entry needed (config/roles.yaml already grants this to roi_clerk
    # and management).
    return _get("roi", f"/roi/patients/{patient_id}/accounting")


# --------------------------------------------------------------------------- #
# interop
# --------------------------------------------------------------------------- #
@app.post("/hl7/ingest")
def proxy_hl7(payload: dict, session: dict = Depends(require_permission("records.write"))):
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
        r = httpx.post(
            f"{SERVICES[service]}{path}",
            json=payload,
            headers=_internal_headers(headers),
            timeout=30,
        )
        data = _safe_json(r)
        if forward_status:
            return JSONResponse(status_code=r.status_code, content=data)
        return data
    except Exception as e:
        log.error("proxy POST %s%s failed: %s", service, path, e)
        if forward_status:
            return JSONResponse(status_code=502, content={"error": str(e)})
        return {"error": str(e)}


def _internal_headers(headers: Optional[dict] = None) -> dict:
    """Every outbound call to an internal service carries the shared token.

    Injected here rather than at each call site deliberately. Branch 7 adds
    token verification to eligibility and scheduling, and those services have
    a dozen call sites between them — adding the header by hand at each one is
    exactly how a single route gets missed and 401s in production while every
    other path works. Centralising it means a new proxy route is covered the
    moment it is written.

    Call sites that already set X-Internal-Token (the records/intake proxies,
    which also forward X-Actor-Id) are left as they are: their explicit value
    wins, because `headers` is applied last.
    """
    merged = {"X-Internal-Token": settings.internal_service_token}
    if headers:
        merged.update(headers)
    return merged


def _get(
    service: str,
    path: str,
    params: Optional[dict] = None,
    *,
    headers: Optional[dict] = None,
    forward_status: bool = False,
):
    try:
        r = httpx.get(
            f"{SERVICES[service]}{path}",
            params=_clean(params),
            headers=_internal_headers(headers),
            timeout=30,
        )
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


# --------------------------------------------------------------------------- #
# the patient's own results (S2)
#
# Note the shape of the path: there is no patient_id in it. The chart this
# returns is derived from the signed-in account's own users.patient_id, so a
# patient cannot even express a request for someone else's results — the class
# of bug RIV-201 was, removed by construction rather than by a check.
#
# Gated on own_record.read, which only the `patient` role holds. Staff roles do
# NOT hold it: a clinician reading a chart goes through the staff routes above,
# where their access is scoped and audited as staff access. Keeping the two
# apart means "a patient read their own record" and "a clinician read a
# patient's record" never collapse into the same audit line.
# --------------------------------------------------------------------------- #
@app.get("/patient/me/identity")
def proxy_own_identity(
    session: dict = Depends(require_permission("own_record.read")),
    db: Session = Depends(get_db),
):
    """The signed-in patient's own name and id — nothing else.

    Same resolution as /patient/me/summary below (session -> users.patient_id
    -> the chart), so this can never return a name for any chart other than
    the one the caller's own account is linked to. `patients.name` (the
    authoritative source per the mirror model above) is what /my-results and
    the approved agent-summary display both read to render the "{Name} —
    Patient ID {id}" format on the patient's own screens, the same string
    shape the staff-side /patients/{id}/name lookup already produces.

    Gated on the same `has_active_grant` check every other chart read in this
    file uses (issuance, revocation) — a live session for an account that was
    since disabled, or whose self-grant (created at activation) was revoked
    or has expired, must not still resolve a name here. `own_record.read`
    only proves the caller is A patient, not that this specific grant is
    still live — code review round 1 (B1) found this route was the one
    identity path that skipped that second check.
    """
    user_id = parse_user_id(session.get("user_id"))
    if user_id is None:
        raise HTTPException(status_code=403, detail="not authorized")
    try:
        patient_id = db.execute(
            select(User.patient_id).where(User.id == user_id)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("own identity: account store unreadable for user_id=%s", user_id)
        raise HTTPException(status_code=503, detail="temporarily unavailable")
    if patient_id is None:
        log.warning("own identity: account has no linked patient user_id=%s", user_id)
        raise HTTPException(status_code=403, detail="not authorized")

    try:
        authorized = has_active_grant(db, user_id=user_id, patient_id=patient_id)
    except SQLAlchemyError:
        log.exception("own identity: grant store unreadable for user_id=%s", user_id)
        raise HTTPException(status_code=503, detail="temporarily unavailable")
    if not authorized:
        raise HTTPException(status_code=403, detail="not authorized")

    try:
        patient = db.execute(select(Patient).where(Patient.id == patient_id)).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("own identity: patient store unreadable for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="temporarily unavailable")
    if patient is None:
        raise HTTPException(status_code=403, detail="not authorized")

    return {"patient_id": patient_id, "name": patient.name}


@app.get("/patient/me/summary")
def proxy_own_results_summary(
    session: dict = Depends(require_permission("own_record.read")),
    db: Session = Depends(get_db),
):
    user_id = parse_user_id(session.get("user_id"))
    if user_id is None:
        raise HTTPException(status_code=403, detail="not authorized")

    try:
        patient_id = db.execute(
            select(User.patient_id).where(User.id == user_id)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("own results: account store unreadable for user_id=%s", user_id)
        raise HTTPException(status_code=503, detail="temporarily unavailable")

    if patient_id is None:
        # An account holding own_record.read but linked to no patient is a
        # misconfiguration, not a patient — refuse rather than fall back to
        # anything. There is no sensible default chart to show.
        log.warning("own results: account has no linked patient user_id=%s", user_id)
        raise HTTPException(status_code=403, detail="not authorized")

    headers = _correlation_headers()
    headers["X-Actor-Id"] = str(user_id)
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get(
        "records",
        f"/patients/{patient_id}/summary",
        headers=headers,
        forward_status=True,
    )


# --------------------------------------------------------------------------- #
# clinician review queue (S3)
#
# Gated on summary_review.decide AND records.read, mirroring records-service.
# The release action has its own permission because neither records.write nor
# the pair read+write excludes the right people: `lab` holds write without
# read, and the deprecated `staff` role — which every seeded account still
# uses — holds both. Only clinician and nursing_ma hold
# summary_review.decide, so this is closed for every existing account rather
# than waiting on the roster-gated migration. See records-service for the
# full reasoning.
# --------------------------------------------------------------------------- #
@app.get("/review-queue")
def proxy_review_queue(
    limit: int = 50,
    session: dict = Depends(require_permission("summary_review.decide")),
    _read: dict = Depends(require_permission("records.read")),
):
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _get(
        "records", "/review-queue", params={"limit": limit}, headers=headers, forward_status=True
    )


@app.post("/review-queue/{review_id}/decision")
def proxy_review_decision(
    review_id: int,
    payload: dict,
    session: dict = Depends(require_permission("summary_review.decide")),
    _read: dict = Depends(require_permission("records.read")),
):
    """Forward a clinician's approve/reject. The decision itself is recorded
    downstream, against the actor this gateway identifies — never against an
    actor the caller supplied."""
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return _post(
        "records",
        f"/review-queue/{review_id}/decision",
        payload,
        headers=headers,
        forward_status=True,
    )


# --------------------------------------------------------------------------- #
# Week 8 agent draft.
#
# The three clinician routes are gated exactly like the review queue
# (summary_review.decide AND records.read) because they ARE the review: the
# draft read returns unapproved text, and generation puts a case in front of
# whoever decides. The patient route is a separate function on own_record.read,
# resolving the chart from the SESSION and never from the path, so a patient
# cannot name someone else's patient_id at all.
# --------------------------------------------------------------------------- #


def _agent_headers(session: dict) -> dict:
    headers = _correlation_headers()
    headers["X-Actor-Id"] = session.get("user_id") or ""
    headers["X-Actor-Name"] = session.get("username") or ""
    headers["X-Internal-Token"] = settings.internal_service_token
    return headers


@app.post("/patients/{patient_id}/agent-draft", status_code=201)
def proxy_generate_agent_draft(
    patient_id: int,
    session: dict = Depends(require_permission("summary_review.decide")),
    _read: dict = Depends(require_permission("records.read")),
):
    return _post("records", f"/patients/{patient_id}/agent-draft", {},
                 headers=_agent_headers(session), forward_status=True)


@app.get("/patients/{patient_id}/agent-draft")
def proxy_get_agent_draft(
    patient_id: int,
    session: dict = Depends(require_permission("summary_review.decide")),
    _read: dict = Depends(require_permission("records.read")),
):
    return _get("records", f"/patients/{patient_id}/agent-draft",
                headers=_agent_headers(session), forward_status=True)


@app.post("/agent-drafts/{draft_id}/decision")
def proxy_decide_agent_draft(
    draft_id: int,
    payload: dict,
    session: dict = Depends(require_permission("summary_review.decide")),
    _read: dict = Depends(require_permission("records.read")),
):
    return _post("records", f"/agent-drafts/{draft_id}/decision", payload,
                 headers=_agent_headers(session), forward_status=True)


@app.post("/policy/ask")
def proxy_ask_policy_navigator(payload: dict, session: dict = Depends(require_session)):
    """w-9-2-planner P3: any authenticated session (staff or patient) may
    ask — there is no patient_id and no data-type permission here, since
    this never touches patient data. records-service re-derives the
    caller's role from X-Actor-Id itself (never trusts a forwarded role)
    and maps it to a read-only audience/workflow scope
    (libs/policy_navigator.scope_for_role) that determines what policy text
    is actually visible; `question` is the only thing forwarded from the
    request body.
    """
    question = payload.get("question") if isinstance(payload, dict) else None
    return _post("records", "/policy/ask", {"question": question},
                 headers=_agent_headers(session), forward_status=True)


@app.get("/patient/me/agent-summary")
def proxy_own_agent_summary(
    session: dict = Depends(require_permission("own_record.read")),
    db: Session = Depends(get_db),
):
    """The signed-in patient's approved summary. Chart resolved from the
    session, mirroring /patient/me/summary — the path carries no patient id, so
    there is nothing for a caller to substitute."""
    user_id = parse_user_id(session.get("user_id"))
    if user_id is None:
        raise HTTPException(status_code=403, detail="not authorized")
    try:
        patient_id = db.execute(
            select(User.patient_id).where(User.id == user_id)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("agent summary: account store unreadable for user_id=%s", user_id)
        raise HTTPException(status_code=503, detail="temporarily unavailable")
    if patient_id is None:
        log.warning("agent summary: account has no linked patient user_id=%s", user_id)
        raise HTTPException(status_code=403, detail="not authorized")
    return _get("records", f"/patients/{patient_id}/agent-summary",
                headers=_agent_headers(session), forward_status=True)


@app.post("/patient/me/agent-summary/request", status_code=201)
def proxy_request_own_agent_summary(
    session: dict = Depends(require_permission("own_record.read")),
    db: Session = Depends(get_db),
):
    """The patient asks for a summary of their own chart.

    Like /patient/me/summary, the chart is resolved from the SESSION and the
    path carries no patient id — there is nothing for the browser to name, so
    there is nothing for it to substitute. Returns a receipt (version, status,
    label, citations, correlation id) and never draft text.
    """
    user_id = parse_user_id(session.get("user_id"))
    if user_id is None:
        raise HTTPException(status_code=403, detail="not authorized")
    try:
        patient_id = db.execute(
            select(User.patient_id).where(User.id == user_id)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("agent summary request: account store unreadable user_id=%s", user_id)
        raise HTTPException(status_code=503, detail="temporarily unavailable")
    if patient_id is None:
        log.warning("agent summary request: account has no linked patient user_id=%s", user_id)
        raise HTTPException(status_code=403, detail="not authorized")
    return _post("records", f"/patients/{patient_id}/agent-summary/request", {},
                 headers=_agent_headers(session), forward_status=True)


# --------------------------------------------------------------------------- #
# W9.2 — secure patient-clinician messaging.
#
# messages.read/messages.write are held by BOTH the patient role and the
# clinician/nursing_ma roles (config/roles.yaml) — the per-patient scoping is
# the grant records-service checks, the same mechanism own_record.read and
# summary_review.decide already piggyback on. So /threads, /threads/{id} and
# its two actions are single shared routes: a patient calling them only ever
# sees their own thread(s), because that is the only patient_id their own
# grant matches, with no separate "self" endpoint required. Only creating a
# NEW thread needs the session-resolved patient_id, the same way requesting
# an agent summary does above — see proxy_create_own_thread.
# --------------------------------------------------------------------------- #


@app.get("/threads")
def proxy_list_threads(
    limit: int = 50,
    session: dict = Depends(require_permission("messages.read")),
):
    return _get("records", "/threads", params={"limit": limit},
                headers=_agent_headers(session), forward_status=True)


@app.get("/threads/{thread_id}")
def proxy_get_thread(
    thread_id: int,
    session: dict = Depends(require_permission("messages.read")),
):
    return _get("records", f"/threads/{thread_id}",
                headers=_agent_headers(session), forward_status=True)


@app.post("/threads/{thread_id}/messages", status_code=201)
def proxy_reply_to_thread(
    thread_id: int,
    payload: dict,
    session: dict = Depends(require_permission("messages.write")),
):
    return _post("records", f"/threads/{thread_id}/messages", payload,
                 headers=_agent_headers(session), forward_status=True)


@app.post("/threads/{thread_id}/status")
def proxy_set_thread_status(
    thread_id: int,
    payload: dict,
    session: dict = Depends(require_permission("messages.write")),
):
    """Close/reopen. records-service enforces staff-only on top of the grant
    check — a patient holding messages.write can still reply, just not
    change the thread's lifecycle."""
    return _post("records", f"/threads/{thread_id}/status", payload,
                 headers=_agent_headers(session), forward_status=True)


@app.post("/patient/me/threads", status_code=201)
def proxy_create_own_thread(
    payload: dict,
    session: dict = Depends(require_permission("messages.write")),
    db: Session = Depends(get_db),
):
    """A patient starts a new thread. Same session-resolution as
    /patient/me/agent-summary/request above — the chart comes from the
    account, never from anything the browser supplies."""
    user_id = parse_user_id(session.get("user_id"))
    if user_id is None:
        raise HTTPException(status_code=403, detail="not authorized")
    try:
        patient_id = db.execute(
            select(User.patient_id).where(User.id == user_id)
        ).scalar_one_or_none()
    except SQLAlchemyError:
        log.exception("messages: account store unreadable user_id=%s", user_id)
        raise HTTPException(status_code=503, detail="temporarily unavailable")
    if patient_id is None:
        log.warning("messages: account has no linked patient user_id=%s", user_id)
        raise HTTPException(status_code=403, detail="not authorized")
    return _post("records", f"/patients/{patient_id}/threads", payload,
                 headers=_agent_headers(session), forward_status=True)
