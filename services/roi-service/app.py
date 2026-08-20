"""
roi-service — Release of Information (ROI) request intake + disclosures.

Replaces the old "staff emails a PDF" workflow: create an ROI request, fulfill
it (release records to the named recipient), and read back what was disclosed.
"""
import hmac
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from logging_config import configure
from models import Disclosure, Patient, Record, RoiRequest
from schemas import (
    DisclosureRecords,
    FulfillResult,
    RecordOut,
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
    It is NOT per-resource authorization, and this branch does not claim to
    add that. See the PR body for what remains deferred.
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


@app.post("/roi/requests/{request_id}/fulfill", response_model=FulfillResult, dependencies=[Depends(_verify_internal_token)])
def fulfill_roi_request(request_id: int, db: Session = Depends(get_db)):
    """
    Fulfill an ROI request: mark it 'fulfilled', record a disclosures row, and
    return the patient's records.

    ==========================================================================
    DEBT D12 — HIPAA Privacy Rule shortcuts (deliberate, do NOT "fix"):
      * NO check for a signed 45 CFR 164.508 authorization before releasing PHI.
      * NO honoring of any 45 CFR 164.522 agreed restriction on the patient.
      * NO accounting-of-disclosures audit_logs entry is written. The only trace
        is the bare disclosures row below, which itself has no authorization_id,
        no purpose, and no restriction tracking — so a true 164.528 accounting
        of disclosures cannot be produced.
    ==========================================================================
    """
    try:
        req = db.get(RoiRequest, request_id)
        if req is None:
            raise HTTPException(status_code=404, detail="roi request not found")

        # D12: release happens with no authorization / restriction enforcement.
        req.status = "fulfilled"

        disclosure = Disclosure(
            patient_id=req.patient_id,
            roi_request_id=req.id,
            disclosed_to=req.recipient,
            # no authorization_id, no purpose, no restriction tracking
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
        raise
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


@app.get("/disclosures/{patient_id}", response_model=DisclosureRecords, dependencies=[Depends(_verify_internal_token)])
def disclose(patient_id: int, db: Session = Depends(get_db)):
    """
    Legacy direct-disclosure surface (original D12).

    DEBT D12 (preserved): returns ALL of a patient's records with NO check for a
    valid 45 CFR 164.508 authorization, NO honoring of any 164.522 agreed
    restriction, and NO disclosure logged (who got what, when, under what
    authorization) — so an accounting-of-disclosures is impossible.
    """
    try:
        rows = (
            db.execute(
                select(Record)
                .where(Record.patient_id == patient_id)
                .order_by(Record.id)
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError:
        log.exception("disclose: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    return DisclosureRecords(
        patient_id=patient_id,
        records=[RecordOut.model_validate(r) for r in rows],
    )
