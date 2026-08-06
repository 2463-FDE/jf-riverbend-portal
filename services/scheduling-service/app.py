"""
scheduling-service — appointment slots (FHIR Appointment / Slot shaped).

Read endpoints use the SQLAlchemy ORM. Booking goes through the raw-psycopg2
path in book.py, not the ORM — kept that way through the Stage 4 (RIV-175)
fix because book()'s SAVEPOINT-based unique-violation handling needs direct
control over statement-level error recovery within one transaction that the
ORM's session/unit-of-work model doesn't offer as directly.
"""
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from book import IdempotencyKeyConflict, book
from config import settings
from db import get_db
from logging_config import configure
from models import Appointment, Provider, Slot
from schemas import (
    AppointmentListResponse,
    AppointmentOut,
    BookingRequest,
    BookingResponse,
    CancelResponse,
    SlotListResponse,
    SlotOut,
)

log = configure(settings.service_name)

app = FastAPI(title="Riverbend scheduling-service")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.service_name}


@app.get("/slots", response_model=SlotListResponse)
def list_slots(
    provider_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(settings.default_page_limit, ge=1, le=settings.max_page_limit),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List open slots, joined to the provider name. Paginated."""
    stmt = (
        select(Slot, Provider.name)
        .join(Provider, Provider.id == Slot.provider_id, isouter=True)
        .where(Slot.status == "open")
    )
    if provider_id is not None:
        stmt = stmt.where(Slot.provider_id == provider_id)
    stmt = stmt.order_by(Slot.start_at).limit(limit).offset(offset)

    try:
        rows = db.execute(stmt).all()
    except Exception:
        log.exception("failed to list slots")
        raise HTTPException(status_code=503, detail="database unavailable")

    items = []
    for slot, provider_name in rows:
        out = SlotOut.model_validate(slot)
        out.provider = provider_name
        items.append(out)

    log.info("listed %d open slots (provider_id=%s)", len(items), provider_id)
    return SlotListResponse(items=items, count=len(items), limit=limit, offset=offset)


@app.get("/appointments", response_model=AppointmentListResponse)
def list_appointments(
    patient_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
):
    """List a patient's appointments, most recent first."""
    stmt = (
        select(Appointment)
        .where(Appointment.patient_id == patient_id)
        .order_by(Appointment.created_at.desc())
    )
    try:
        rows = db.execute(stmt).scalars().all()
    except Exception:
        log.exception("failed to list appointments for patient %s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    items = [AppointmentOut.model_validate(a) for a in rows]
    log.info("listed %d appointments for patient %s", len(items), patient_id)
    return AppointmentListResponse(items=items, count=len(items))


@app.post("/appointments", status_code=201, response_model=BookingResponse)
def create_appointment(req: BookingRequest):
    """Book a slot for a patient.

    Stage 4 (Week 5, RIV-175, migration 013): book.py does the idempotency
    check and the insert in one transaction, guarded by a real database
    UNIQUE index (at most one confirmed appointment per slot) — see book.py's
    own docstring for the full design. Only two outcomes get the 201 this
    route is declared with: a fresh booking, or a genuine replay of the
    SAME request via the same idempotency_key (a retry of a slow POST must
    not look like a failure to the caller). Everything else the route can
    detect is a real failure, not a variant of success — round-22 review
    (2026-08-06): a losing concurrent booking used to get 201 with
    status="slot_taken" in the body, and the frontend's `if (!r.ok)` check
    never caught it, so a losing booker saw "appointment booked."
    """
    try:
        appointment_id, is_replay = book(
            req.patient_id,
            req.slot_id,
            req.idempotency_key,
            provider=req.provider,
            reason=req.reason,
            location=req.location,
            scheduled_for=req.scheduled_for,
        )
    except IdempotencyKeyConflict as e:
        log.warning(
            "booking rejected: idempotency_key reused with different booking details "
            "(patient=%s, existing_appointment_id=%s)",
            req.patient_id, e.existing_appointment_id,
        )
        raise HTTPException(
            status_code=409,
            detail={"error": "idempotency_key_conflict", "existing_appointment_id": e.existing_appointment_id},
        )
    except Exception:
        log.exception(
            "booking failed for patient=%s slot=%s", req.patient_id, req.slot_id
        )
        raise HTTPException(status_code=503, detail="database unavailable")

    if appointment_id is None:
        log.info("slot %s already taken (patient=%s)", req.slot_id, req.patient_id)
        raise HTTPException(status_code=409, detail={"error": "slot_taken"})

    log.info(
        "booked appointment %s (patient=%s slot=%s replay=%s)",
        appointment_id,
        req.patient_id,
        req.slot_id,
        is_replay,
    )
    return BookingResponse(appointment_id=appointment_id, status="confirmed")


@app.post("/appointments/{appointment_id}/cancel", response_model=CancelResponse)
def cancel_appointment(appointment_id: int, db: Session = Depends(get_db)):
    """Cancel an appointment. 404 if it does not exist."""
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="appointment not found")

    appt.status = "cancelled"
    try:
        db.commit()
    except Exception:
        db.rollback()
        log.exception("failed to cancel appointment %s", appointment_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    log.info("cancelled appointment %s", appointment_id)
    return CancelResponse(appointment_id=appointment_id, status="cancelled")
