"""
scheduling-service — appointment slots (FHIR Appointment / Slot shaped).

Read endpoints use the SQLAlchemy ORM. Booking goes through the raw-psycopg2
path in book.py, not the ORM — kept that way through the Stage 4 (RIV-175)
fix because book()'s SAVEPOINT-based unique-violation handling needs direct
control over statement-level error recovery within one transaction that the
ORM's session/unit-of-work model doesn't offer as directly.
"""
import hmac
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Query
from sqlalchemy import exists, func, select
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


_MIN_INTERNAL_TOKEN_LENGTH = 32  # rejects "changeme" and any other short/example value


def _internal_token_is_configured() -> bool:
    """The same presence/length floor _verify_internal_token enforces per
    request, checked once at startup so a misconfigured deploy fails loudly
    instead of serving traffic that 401s every gateway-forwarded call. Mirrors
    intake-service and records-service."""
    configured = settings.internal_service_token
    return bool(configured) and len(configured) >= _MIN_INTERNAL_TOKEN_LENGTH


def _verify_internal_token(x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token")) -> None:
    """Prove this call came through the gateway.

    Cycle branch 7. This service published its port to the host and verified
    no caller at all, so anything able to reach the host could call it
    directly and bypass every permission check the gateway applies. The port
    is no longer published, but that is containment, not authentication — a
    caller already inside the compose network was still trusted blind.

    Mirrors services/intake-service/app.py::_verify_internal_token exactly:
    same shared INTERNAL_SERVICE_TOKEN, same fail-closed semantics. An
    unset/empty configured token, or a human-typed placeholder shorter than
    _MIN_INTERNAL_TOKEN_LENGTH, is never treated as "no check needed".

    This is transport trust — it proves the call arrived through the gateway.
    It is NOT per-resource authorization, and this branch does not claim to
    add that.
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

    The docstring on _internal_token_is_configured claimed this happened;
    nothing called it, so the function was dead code and this service would
    have started cleanly with a placeholder token and then rejected every
    request — the exact healthy-looking outage the round-13/17 reviews fixed
    for gateway, intake-service and records-service.

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



@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.service_name}


@app.get("/slots", response_model=SlotListResponse, dependencies=[Depends(_verify_internal_token)])
def list_slots(
    provider_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(settings.default_page_limit, ge=1, le=settings.max_page_limit),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List open slots, joined to the provider name. Paginated.

    w9-fixes P0 4.2: `slots.status` is the read model and `appointments` is
    the write model, and before this fix nothing kept them in sync on a
    confirmed booking — the live demo database had a large, pre-existing
    batch of slots marked 'open' with a confirmed appointment sitting on
    them (legacy seed inconsistency, not test pollution — see
    db/seed/generate_seed.py's history). book.py's own INSERT now updates
    slots.status in the same transaction (closing the gap going forward),
    but this excludes a slot with ANY confirmed appointment regardless of
    its stored status column, so a slot from BEFORE that fix (or any other
    drift) fails closed instead of being offered again.

    Also requires start_at in the future: a slot's status/appointment state
    can be perfectly consistent and it still must never be offered once its
    time has passed. This is also what keeps the seed's dedicated
    demo-booking pool (95001-95016) usable — see db/seed/demo_reset.sql,
    which is the thing that actually keeps those ids' start_at ahead of
    "now" between rehearsals.
    """
    stmt = (
        select(Slot, Provider.name)
        .join(Provider, Provider.id == Slot.provider_id, isouter=True)
        .where(
            Slot.status == "open",
            Slot.start_at > func.now(),
            ~exists().where(Appointment.slot_id == Slot.id, Appointment.status == "confirmed"),
        )
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


@app.get("/appointments", response_model=AppointmentListResponse, dependencies=[Depends(_verify_internal_token)])
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


@app.post("/appointments", status_code=201, response_model=BookingResponse, dependencies=[Depends(_verify_internal_token)])
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
        # w9-fixes P0 4.2/4.3: provider/location/scheduled_for are no longer
        # passed through from the request — book() derives all three from
        # the locked slot row itself now, never from the caller. See that
        # module's docstring for why (a client-supplied time/location/
        # provider could disagree with the slot actually being booked).
        appointment_id, is_replay = book(
            req.patient_id,
            req.slot_id,
            req.idempotency_key,
            reason=req.reason,
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


@app.post("/appointments/{appointment_id}/cancel", response_model=CancelResponse, dependencies=[Depends(_verify_internal_token)])
def cancel_appointment(appointment_id: int, db: Session = Depends(get_db)):
    """Cancel an appointment. 404 if it does not exist.

    w9-fixes P0 4.2: book() marks a slot 'booked' on confirmation now, so
    cancellation is the other half — it must reopen the slot, or a
    cancelled appointment would leave its slot permanently unbookable by
    anyone. Only meaningful for a still-'confirmed' appointment: cancelling
    an already-cancelled one a second time must not re-open a slot that may
    since have been won by a different, still-confirmed booking.
    """
    appt = db.get(Appointment, appointment_id)
    if appt is None:
        raise HTTPException(status_code=404, detail="appointment not found")

    was_confirmed = appt.status == "confirmed"
    appt.status = "cancelled"
    if was_confirmed:
        slot = db.get(Slot, appt.slot_id)
        if slot is not None:
            slot.status = "open"
    try:
        db.commit()
    except Exception:
        db.rollback()
        log.exception("failed to cancel appointment %s", appointment_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    log.info("cancelled appointment %s", appointment_id)
    return CancelResponse(appointment_id=appointment_id, status="cancelled")
