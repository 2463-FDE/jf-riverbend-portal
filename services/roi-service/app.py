"""
roi-service — Release of Information (ROI) request intake + disclosures.

Replaces the old "staff emails a PDF" workflow: create an ROI request, fulfill
it (release records to the named recipient), and read back what was disclosed.
"""
import hmac
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from logging_config import configure
from models import Disclosure, Patient, Record, RoiAuthorization, RoiDisclosureRestriction, RoiRequest
from schemas import (
    AuthorizationCreate,
    AuthorizationOut,
    AuthorizationRevoke,
    AuthorizationReview,
    DisclosureAccounting,
    DisclosureAccountingEntry,
    FulfillRequest,
    FulfillResult,
    RecordOut,
    RestrictionCreate,
    RestrictionOut,
    RoiRequestCreate,
    RoiRequestOut,
)

log = configure(settings.service_name)

app = FastAPI(title="Riverbend roi-service")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.service_name}


_MIN_INTERNAL_TOKEN_LENGTH = 32  # rejects "changeme" and any other short/example value


def _internal_token_is_configured() -> bool:
    """The same presence/length floor _verify_internal_token enforces per
    request, checked once at startup so a misconfigured deploy fails loudly
    instead of serving traffic that 401s every gateway-forwarded call. Mirrors
    intake-service, records-service, eligibility-service and
    scheduling-service."""
    configured = settings.internal_service_token
    return bool(configured) and len(configured) >= _MIN_INTERNAL_TOKEN_LENGTH


def _verify_internal_token(x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token")) -> None:
    """Prove this call came through the gateway.

    Cycle branch 7B. This service verified no caller at all, so anything able
    to reach it on the compose network could call it directly and bypass every
    permission check the gateway applies. #39 unpublished its host port, but
    that is containment, not authentication — a caller already inside the
    network was still trusted blind, with a forged X-Actor-Id if it liked.

    Mirrors services/eligibility-service/app.py::_verify_internal_token
    exactly: same shared INTERNAL_SERVICE_TOKEN, same fail-closed semantics.
    An unset/empty configured token, or a human-typed placeholder shorter than
    _MIN_INTERNAL_TOKEN_LENGTH, is never treated as "no check needed".

    This is transport trust — it proves the call arrived through the gateway.
    It is NOT per-resource authorization on its own; w8-planner-2's
    authorization/restriction checks below are what add that for the release
    path specifically. A valid internal token alone never bypasses them —
    see disclose() below, which proves that for the legacy route.
    """
    configured = settings.internal_service_token
    if (
        not configured
        or len(configured) < _MIN_INTERNAL_TOKEN_LENGTH
        or not x_internal_token
        or not hmac.compare_digest(x_internal_token, configured)
    ):
        raise HTTPException(status_code=401, detail="missing or invalid internal service token")


@app.on_event("startup")
def _fail_fast_on_an_unusable_token() -> None:
    """Refuse to start rather than serve traffic that 401s everything.

    Compose's ${INTERNAL_SERVICE_TOKEN:?...} stops an entirely MISSING value
    before any container starts. It cannot catch a value that is present but
    unusable — "changeme", or anything under the length floor — which is
    precisely the case this check exists for.
    """
    if not _internal_token_is_configured():
        raise RuntimeError(
            f"INTERNAL_SERVICE_TOKEN is not set (or is shorter than "
            f"{_MIN_INTERNAL_TOKEN_LENGTH} chars) — refusing to start. Set a real "
            f"random value (e.g. `openssl rand -hex 32`) in .env; see .env.example."
        )


@app.get("/roi/requests", response_model=list[RoiRequestOut], dependencies=[Depends(_verify_internal_token)])
def list_roi_requests(
    patient_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
):
    """List ROI requests, optionally filtered by patient."""
    try:
        stmt = select(RoiRequest)
        if patient_id is not None:
            stmt = stmt.where(RoiRequest.patient_id == patient_id)
        rows = db.execute(stmt.order_by(RoiRequest.id.desc())).scalars().all()
    except SQLAlchemyError:
        log.exception("list_roi_requests: database error")
        raise HTTPException(status_code=503, detail="database unavailable")

    return [RoiRequestOut.model_validate(r) for r in rows]


@app.post("/roi/requests", response_model=RoiRequestOut, status_code=201, dependencies=[Depends(_verify_internal_token)])
def create_roi_request(payload: RoiRequestCreate, db: Session = Depends(get_db)):
    """Create an ROI request. Patient must exist; status defaults to 'pending'."""
    try:
        patient = db.get(Patient, payload.patient_id)
        if patient is None:
            raise HTTPException(status_code=404, detail="patient not found")

        req = RoiRequest(
            patient_id=payload.patient_id,
            requested_by=payload.requested_by,
            recipient=payload.recipient,
            recipient_type=payload.recipient_type,
            purpose=payload.purpose,
            date_range_start=payload.date_range_start,
            date_range_end=payload.date_range_end,
            status="pending",
        )
        db.add(req)
        db.commit()
        db.refresh(req)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("create_roi_request: database error")
        raise HTTPException(status_code=503, detail="database unavailable")

    return RoiRequestOut.model_validate(req)


# --- 164.508 authorization lifecycle (030) ----------------------------------- #


@app.post(
    "/roi/authorizations", response_model=AuthorizationOut, status_code=201,
    dependencies=[Depends(_verify_internal_token)],
)
def create_authorization(payload: AuthorizationCreate, db: Session = Depends(get_db)):
    """Record a submitted authorization. Always starts 'pending' — nothing in
    the payload can mark itself 'valid'; only review_authorization can."""
    try:
        patient = db.get(Patient, payload.patient_id)
        if patient is None:
            raise HTTPException(status_code=404, detail="patient not found")

        auth = RoiAuthorization(
            patient_id=payload.patient_id,
            recipient=payload.recipient,
            purpose=payload.purpose,
            scope_start=payload.scope_start,
            scope_end=payload.scope_end,
            signature_evidence_reference=payload.signature_evidence_reference,
            signature_evidence_digest=payload.signature_evidence_digest,
            signed_by=payload.signed_by,
            signed_at=payload.signed_at,
            expires_at=payload.expires_at,
            representative_authority=payload.representative_authority,
            status="pending",
        )
        db.add(auth)
        db.commit()
        db.refresh(auth)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("create_authorization: database error")
        raise HTTPException(status_code=503, detail="database unavailable")

    return AuthorizationOut.model_validate(auth)


@app.get(
    "/roi/authorizations/{authorization_id}", response_model=AuthorizationOut,
    dependencies=[Depends(_verify_internal_token)],
)
def get_authorization(authorization_id: int, db: Session = Depends(get_db)):
    try:
        auth = db.get(RoiAuthorization, authorization_id)
    except SQLAlchemyError:
        log.exception("get_authorization: database error for authorization_id=%s", authorization_id)
        raise HTTPException(status_code=503, detail="database unavailable")
    if auth is None:
        raise HTTPException(status_code=404, detail="authorization not found")
    return AuthorizationOut.model_validate(auth)


@app.post(
    "/roi/authorizations/{authorization_id}/review", response_model=AuthorizationOut,
    dependencies=[Depends(_verify_internal_token)],
)
def review_authorization(authorization_id: int, payload: AuthorizationReview, db: Session = Depends(get_db)):
    """The ONLY path an authorization can reach 'valid' through. Refuses a
    second review of an authorization that already left 'pending' — a
    reviewed decision does not get silently overwritten by another review
    call; use revoke_authorization to withdraw an already-valid one."""
    try:
        auth = db.get(RoiAuthorization, authorization_id)
        if auth is None:
            raise HTTPException(status_code=404, detail="authorization not found")
        if auth.status != "pending":
            raise HTTPException(status_code=409, detail=f"authorization already reviewed (status={auth.status!r})")

        auth.status = payload.decision
        auth.reviewed_by = payload.reviewed_by
        auth.reviewed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(auth)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("review_authorization: database error for authorization_id=%s", authorization_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    return AuthorizationOut.model_validate(auth)


@app.post(
    "/roi/authorizations/{authorization_id}/revoke", response_model=AuthorizationOut,
    dependencies=[Depends(_verify_internal_token)],
)
def revoke_authorization(authorization_id: int, payload: AuthorizationRevoke, db: Session = Depends(get_db)):
    """Withdraws a previously valid authorization. A pending/rejected one
    cannot be revoked — there is nothing valid to withdraw; an already-revoked
    one is a no-op-with-409, not a silently repeated revocation."""
    try:
        auth = db.get(RoiAuthorization, authorization_id)
        if auth is None:
            raise HTTPException(status_code=404, detail="authorization not found")
        if auth.status != "valid":
            raise HTTPException(status_code=409, detail=f"cannot revoke an authorization with status={auth.status!r}")

        auth.status = "revoked"
        auth.revoked_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(auth)
        log.info("authorization revoked (authorization_id=%s revoked_by=%s)", authorization_id, payload.revoked_by)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("revoke_authorization: database error for authorization_id=%s", authorization_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    return AuthorizationOut.model_validate(auth)


# --- 164.522 disclosure restrictions (030) ------------------------------------ #


@app.post(
    "/roi/restrictions", response_model=RestrictionOut, status_code=201,
    dependencies=[Depends(_verify_internal_token)],
)
def create_restriction(payload: RestrictionCreate, db: Session = Depends(get_db)):
    """Register a narrowly scoped disclosure restriction. Not a general
    consent-management platform — see models.py::RoiDisclosureRestriction."""
    try:
        patient = db.get(Patient, payload.patient_id)
        if patient is None:
            raise HTTPException(status_code=404, detail="patient not found")

        restriction = RoiDisclosureRestriction(
            patient_id=payload.patient_id,
            recipient=payload.recipient,
            reason=payload.reason,
            active=True,
        )
        db.add(restriction)
        db.commit()
        db.refresh(restriction)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("create_restriction: database error")
        raise HTTPException(status_code=503, detail="database unavailable")

    return RestrictionOut.model_validate(restriction)


@app.post(
    "/roi/restrictions/{restriction_id}/revoke", response_model=RestrictionOut,
    dependencies=[Depends(_verify_internal_token)],
)
def revoke_restriction(restriction_id: int, db: Session = Depends(get_db)):
    try:
        restriction = db.get(RoiDisclosureRestriction, restriction_id)
        if restriction is None:
            raise HTTPException(status_code=404, detail="restriction not found")
        if not restriction.active:
            raise HTTPException(status_code=409, detail="restriction is already inactive")

        restriction.active = False
        restriction.revoked_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(restriction)
    except HTTPException:
        raise
    except SQLAlchemyError:
        db.rollback()
        log.exception("revoke_restriction: database error for restriction_id=%s", restriction_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    return RestrictionOut.model_validate(restriction)


# --- fulfillment: authorization + restriction enforcement, one transaction -- #


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """SQLAlchemy's postgres TIMESTAMPTZ type always round-trips
    timezone-aware datetimes in production, but SQLite (used by the unit
    tests here — there's no live Postgres in this test tier) stores and
    returns naive ones. Normalize rather than let the two backends disagree
    on whether an expiry check is even comparable."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _authorization_error(auth: RoiAuthorization, req: RoiRequest, now: datetime) -> str | None:
    """Every reason a loaded, existing authorization may not be used to
    fulfill this specific request — returns the first violated reason, or
    None if it is good to use. Centralized so fulfill_roi_request has one
    validation path, not a chain of separately-maintained checks."""
    if auth.status == "revoked" or auth.revoked_at is not None:
        return "authorization has been revoked"
    if auth.status == "rejected":
        return "authorization was rejected on review"
    if auth.status == "pending":
        return "authorization has not been reviewed yet"
    if auth.status != "valid":
        return f"authorization status is {auth.status!r}, not 'valid'"
    expires_at = _as_aware_utc(auth.expires_at)
    if expires_at is not None and expires_at <= now:
        return "authorization has expired"
    if auth.patient_id != req.patient_id:
        return "authorization does not cover this patient"
    if auth.recipient != req.recipient:
        return "authorization recipient does not match the request recipient"
    if auth.scope_start and req.date_range_start and req.date_range_start < auth.scope_start:
        return "request date range starts before the authorization's scope"
    if auth.scope_end and req.date_range_end and req.date_range_end > auth.scope_end:
        return "request date range extends beyond the authorization's scope"
    return None


def _active_restriction(db: Session, patient_id: int, recipient: str | None) -> RoiDisclosureRestriction | None:
    return (
        db.execute(
            select(RoiDisclosureRestriction).where(
                RoiDisclosureRestriction.patient_id == patient_id,
                RoiDisclosureRestriction.active.is_(True),
                or_(
                    RoiDisclosureRestriction.recipient.is_(None),
                    RoiDisclosureRestriction.recipient == recipient,
                ),
            )
        )
        .scalars()
        .first()
    )


@app.post("/roi/requests/{request_id}/fulfill", response_model=FulfillResult, dependencies=[Depends(_verify_internal_token)])
def fulfill_roi_request(request_id: int, payload: FulfillRequest, db: Session = Depends(get_db)):
    """
    Fulfill an ROI request: mark it 'fulfilled', record a disclosures row, and
    return the patient's records.

    ==========================================================================
    w8-planner-2 P4 (closes 164.508 for real; corrects an over-claim about
    164.528 — see models.py::Disclosure):
      * `payload` carries ONLY authorization_id — never a caller-supplied
        signer/reference/timestamp (029's original, weaker design). The
        referenced roi_authorizations row must exist, be human-reviewed
        'valid', unexpired, unrevoked, and cover this patient/recipient/
        scope — see _authorization_error. Anything else refuses with 422.
      * An active roi_disclosure_restrictions row for this patient/recipient
        (45 CFR 164.522) refuses fulfillment outright — see
        _active_restriction.
      * Authorization load+validation, restriction check, disclosure
        creation, and the request's own status update all happen inside
        this ONE transaction/session — a failure at any point rolls back
        the whole thing, never a partial effect.
      * Idempotent: an already-'fulfilled' request is refused (409), not
        silently re-fulfilled into a second disclosure row.

    W10 Final Stage 2 (remaining concurrency gap): the read above used to be
    a plain `db.get`, with no lock — two simultaneous requests for the SAME
    request_id could both read status='pending' before either committed,
    both pass every check below, and both insert a disclosure row. The
    `SELECT ... FOR UPDATE` here makes the second request block until the
    first's transaction ends, so it re-reads status='fulfilled' and takes
    the 409 path above instead. `disclosures.roi_request_id` also now
    carries a partial UNIQUE index (migration 035) as the database-level
    backstop: even if two transactions somehow raced past the lock (a
    different isolation level, a direct caller bypassing this route), the
    second INSERT itself is rejected — caught below and turned into the
    same truthful 409, never a raw 500.
    ==========================================================================
    """
    try:
        req = db.execute(
            select(RoiRequest).where(RoiRequest.id == request_id).with_for_update()
        ).scalar_one_or_none()
        if req is None:
            raise HTTPException(status_code=404, detail="roi request not found")
        if req.status == "fulfilled":
            raise HTTPException(status_code=409, detail="roi request has already been fulfilled")

        auth = db.get(RoiAuthorization, payload.authorization_id)
        if auth is None:
            raise HTTPException(status_code=404, detail="authorization not found")

        reason = _authorization_error(auth, req, datetime.now(timezone.utc))
        if reason is not None:
            raise HTTPException(status_code=422, detail=reason)

        restriction = _active_restriction(db, req.patient_id, req.recipient)
        if restriction is not None:
            raise HTTPException(status_code=422, detail="an active disclosure restriction blocks this release")

        req.status = "fulfilled"
        req.authorization_id = auth.id
        req.authorization_reference = auth.signature_evidence_reference
        req.authorization_signed_at = auth.signed_at
        req.authorization_signed_by = auth.signed_by

        disclosure = Disclosure(
            patient_id=req.patient_id,
            roi_request_id=req.id,
            authorization_id=auth.id,
            disclosed_to=req.recipient,
            authorization_reference=auth.signature_evidence_reference,
            purpose=auth.purpose or req.purpose,
        )
        db.add(disclosure)

        records = (
            db.execute(
                select(Record)
                .where(Record.patient_id == req.patient_id)
                .order_by(Record.id)
            )
            .scalars()
            .all()
        )

        db.commit()
        db.refresh(disclosure)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        # Backstop for disclosures_roi_request_id_unique (migration 035):
        # the FOR UPDATE lock above should make this unreachable in normal
        # operation, but if a second disclosure for this request_id is ever
        # attempted anyway, report it the same truthful way as the ordinary
        # already-fulfilled case rather than a raw 500.
        db.rollback()
        raise HTTPException(status_code=409, detail="roi request has already been fulfilled")
    except SQLAlchemyError:
        db.rollback()
        log.exception("fulfill_roi_request: database error for request_id=%s", request_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    return FulfillResult(
        request_id=req.id,
        patient_id=req.patient_id,
        status=req.status,
        disclosure_id=disclosure.id,
        records=[RecordOut.model_validate(r) for r in records],
    )


@app.get(
    "/roi/patients/{patient_id}/accounting",
    response_model=DisclosureAccounting,
    dependencies=[Depends(_verify_internal_token)],
)
def disclosure_accounting(patient_id: int, db: Session = Depends(get_db)):
    """
    Every disclosure this service has fulfilled for one patient — an
    internal log, NOT a formal 45 CFR 164.528 accounting of disclosures.
    164.528(a)(2) exempts disclosures made pursuant to a valid 164.508
    authorization from that mandatory accounting requirement, which is
    exactly what every row behind this endpoint is (fulfill_roi_request
    refuses to create one any other way). See models.py::Disclosure for the
    full explanation of what a true 164.528 accounting would still need
    (the non-exempt disclosure categories this system does not model) and
    why this endpoint is not a substitute for one.

    Deliberately does NOT read the legacy disclose() route below — that
    route is retired (410) and never wrote a disclosures row even before
    retirement, so it has nothing to contribute here.
    """
    try:
        rows = (
            db.execute(
                select(Disclosure)
                .where(Disclosure.patient_id == patient_id)
                .order_by(Disclosure.disclosed_at)
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError:
        log.exception("disclosure_accounting: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    return DisclosureAccounting(
        patient_id=patient_id,
        disclosures=[DisclosureAccountingEntry.model_validate(d) for d in rows],
    )


@app.get("/disclosures/{patient_id}", dependencies=[Depends(_verify_internal_token)])
def disclose(patient_id: int):
    """
    RETIRED (w8-planner-2 P4). This was the original DEBT D12 shortcut:
    returned ALL of a patient's records with no authorization check, no
    restriction check, and no disclosure logged. "Not exposed through the
    UI" was never a security boundary — a caller with a valid internal
    service token could still reach it directly, same as any other route
    on this service. Retired outright rather than routed through the new
    authorization/restriction checks: nothing in this codebase calls it
    (the gateway never proxied it), so there is no real caller to migrate,
    and every legitimate release path goes through
    POST /roi/requests/{id}/fulfill instead.
    """
    raise HTTPException(
        status_code=410,
        detail="this route is retired; use POST /roi/requests/{id}/fulfill",
    )
