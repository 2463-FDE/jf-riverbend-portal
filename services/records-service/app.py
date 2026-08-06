"""
records-service — patient + records read façade (FHIR-ish).

Serves patient demographics and a patient's encounters/records to the portal.

Stage 3 addition: `GET /patients/{id}/view`, a new, additive route that wires
the Week 4/5 `libs.patient_view_agent` supervisor to real data — a bounded,
evidence-cited chart summary gated by `StaffAccessGate` (an authenticated-
staff access gate, NOT patient-specific authorization) instead of that
library's fixture `FakePolicyAuthorization`/`SeededChartRepository`. This
does not touch or remediate `get_patient_records`/`get_patient` below
(DEBT D11 / RIV-201) — see docs/analysis/RIV-201-patient-records-IDOR.md.
"""
import hmac
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from libs.patient_view_agent import (
    Action,
    AuthorizationDenied,
    AuthorizationRequest,
    PatientViewResult,
    Purpose,
    StaffAccessGate,
    run_patient_view,
)
from logging_config import configure
from models import AuditLog, Encounter, Patient, Record
from patient_view_repository import SqlChartRepository
from schemas import (
    EncounterOut,
    EncounterWithRecords,
    PatientChart,
    PatientDetail,
    PatientPage,
    PatientSummary,
    RecordOut,
    RecordSearchHit,
)

log = configure(settings.service_name)

app = FastAPI(title="Riverbend records-service")

_STAFF_ACCESS_GATE = StaffAccessGate()


def _internal_token_is_configured() -> bool:
    """Round-13 review (2026-08-06, PR #20): the same presence/length floor
    _verify_internal_token enforces on every request, checked once here so
    /healthz can fail before a misconfigured deploy ever serves a real
    request — see gateway's and intake-service's identical fix."""
    configured = settings.internal_service_token
    return bool(configured) and len(configured) >= _MIN_INTERNAL_TOKEN_LENGTH


@app.get("/healthz")
def healthz():
    if not _internal_token_is_configured():
        raise HTTPException(status_code=503, detail="internal_service_token not configured")
    return {"status": "ok", "service": settings.service_name}


@app.get("/patients", response_model=PatientPage)
def list_patients(
    q: str | None = Query(default=None, description="ILIKE filter on patient name"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Paginated patient list. `q` does a case-insensitive name match."""
    try:
        base = select(Patient)
        count_q = select(func.count()).select_from(Patient)
        if q:
            pattern = f"%{q}%"
            base = base.where(Patient.name.ilike(pattern))
            count_q = count_q.where(Patient.name.ilike(pattern))

        total = db.execute(count_q).scalar_one()
        rows = (
            db.execute(
                base.order_by(Patient.id).limit(limit).offset(offset)
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError:
        log.exception("list_patients: database error")
        raise HTTPException(status_code=503, detail="database unavailable")

    return PatientPage(
        items=[PatientSummary.model_validate(p) for p in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get("/patients/{patient_id}", response_model=PatientDetail)
def get_patient(patient_id: int, db: Session = Depends(get_db)):
    """Patient demographics, or 404."""
    try:
        patient = db.get(Patient, patient_id)
    except SQLAlchemyError:
        log.exception("get_patient: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    return PatientDetail.model_validate(patient)


@app.get("/patients/{patient_id}/records", response_model=PatientChart)
def get_patient_records(patient_id: int, db: Session = Depends(get_db)):
    """
    Assemble a patient's full chart: their encounters and, per encounter, its records.

    DEBT D11 (IDOR): patient_id is the sequential integer PK and is served to any
    caller with no verification that the caller owns / is authorized for it. A
    logged-in user can walk 1042, 1043, 1044... and pull anyone's chart.

    DEBT D8 (N+1): encounters are fetched first, then we loop and run ONE query per
    encounter to load that encounter's records (no JOIN, no selectinload).
    """
    # no ownership / authorization check
    try:
        encounters = (
            db.execute(
                select(Encounter)
                .where(Encounter.patient_id == patient_id)
                .order_by(Encounter.id)
            )
            .scalars()
            .all()
        )

        chart: list[EncounterWithRecords] = []
        # N+1: one extra query per encounter (deliberate — do not collapse to a join)
        for enc in encounters:
            recs = (
                db.execute(
                    select(Record)
                    .where(Record.encounter_id == enc.id)
                    .order_by(Record.id)
                )
                .scalars()
                .all()
            )
            chart.append(
                EncounterWithRecords(
                    encounter=EncounterOut.model_validate(enc),
                    records=[RecordOut.model_validate(r) for r in recs],
                )
            )
    except SQLAlchemyError:
        log.exception(
            "get_patient_records: database error for patient_id=%s", patient_id
        )
        raise HTTPException(status_code=503, detail="database unavailable")

    return PatientChart(patient_id=patient_id, encounters=chart)


def _write_audit(db: Session, *, actor: str, message: str) -> None:
    """Review fix (round 2, 2026-08-05): audit_logs is not tamper-evident
    (db/schema.sql), but it is the only durable record this route produces of
    who accessed a patient's chart. A prior version of this function
    swallowed a write failure and let the caller's request succeed anyway —
    a chart view (or a denial) could complete with zero trace of it ever
    happening. Fails closed instead: an audit write that doesn't commit turns
    into a 503 for the whole request, matching this file's existing
    "database unavailable" convention (see get_patient/get_patient_records/
    search_records above) — the caller never sees chart data (or even a
    confirmed denial) that this service couldn't also durably record.
    """
    try:
        db.add(AuditLog(actor=actor, message=message))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        log.exception("patient_view: failed to write audit_logs entry")
        raise HTTPException(status_code=503, detail="database unavailable")


# Review fix (round 2, 2026-08-05): a real INTERNAL_SERVICE_TOKEN is expected
# to be a long random value (e.g. `openssl rand -hex 32` = 64 chars); this
# floor exists specifically to reject short, human-typed placeholder values
# like "changeme" (8 chars) that a naive deployment might type in by hand
# even though .env.example now ships this var empty, not pre-filled.
_MIN_INTERNAL_TOKEN_LENGTH = 32


def _verify_internal_token(x_internal_token: Optional[str]) -> None:
    """Review fix (round 1, 2026-08-05): `X-Actor-Id` alone is a caller-controlled
    header, not proof the request came from the gateway. records-service's
    port is published to the host (docker-compose.yml), so without this check
    anyone could hit this route directly with `X-Actor-Id: anything` and
    StaffAccessGate would allow it — unauthenticated chart access, plus an
    audit_logs row attributed to a spoofed actor.

    Fails closed multiple ways: an unconfigured `INTERNAL_SERVICE_TOKEN`
    (empty on both sides) must never be treated as "no check needed" — that
    would let the same bypass through via two matching empty strings. Round 2
    (2026-08-05) added the length floor below after review found
    `.env.example`'s original `changeme` placeholder was itself a valid-
    looking secret a real deployment could ship unmodified — a short,
    human-typed value like that must be rejected as confidently as an empty
    one, not just compared byte-for-byte. Raises before any audit_logs write,
    so a rejected direct-access attempt is not recorded under whatever actor
    name the caller supplied.
    """
    configured = settings.internal_service_token
    if (
        not configured
        or len(configured) < _MIN_INTERNAL_TOKEN_LENGTH
        or not x_internal_token
        or not hmac.compare_digest(x_internal_token, configured)
    ):
        raise HTTPException(status_code=401, detail="missing or invalid internal service token")


@app.get("/patients/{patient_id}/view", response_model=PatientViewResult)
def get_patient_view(
    patient_id: int,
    purpose: str = Query(default="treatment"),
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """
    Stage 3 — bounded, evidence-cited patient-chart view via
    `libs.patient_view_agent.run_patient_view`, backed by real data
    (`SqlChartRepository`) and gated by `StaffAccessGate`.

    Two layers of trust, in order: (1) `_verify_internal_token` proves this
    call actually came from the gateway (not a direct caller hitting this
    service's published host port with a spoofed `X-Actor-Id`); (2)
    `StaffAccessGate` is an authenticated-staff access gate, NOT
    patient-specific authorization — once (1) passes, it ALLOWs any request
    carrying a non-empty `X-Actor-Id` and DENIES an unknown/missing one. It
    does not, and cannot, verify that `x_actor_id` is entitled to
    `patient_id` specifically — `users` has no relationship to `patients` in
    this schema (see docs/analysis/RIV-201-patient-records-IDOR.md §6). This
    route does not fix RIV-201; `get_patient_records`/`get_patient` below
    remain exactly as IDOR-exploitable as documented.
    """
    _verify_internal_token(x_internal_token)

    try:
        purpose_enum = Purpose(purpose)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid purpose '{purpose}'")

    # Round-15 review (2026-08-06): SqlChartRepository's first read only
    # loads encounters for patient_id — an unknown id returns an empty
    # result set, not an error, so run_patient_view would previously produce
    # a normal "no evidence" COMPLETED/escalated result for a patient that
    # does not exist at all. A typo'd or stale patient_id then looked
    # identical to a real patient with an empty chart, which is a clinical
    # safety problem for a chart summary a clinician is meant to trust.
    # Checked here, same 404 idiom get_patient already uses above, before
    # authorization or any chart read runs. No audit_logs row is written for
    # a nonexistent id — same reasoning as _verify_internal_token's own
    # rejection path: this is a not-found response, not a chart access.
    try:
        patient_exists = db.get(Patient, patient_id) is not None
    except SQLAlchemyError:
        log.exception("get_patient_view: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")
    if not patient_exists:
        raise HTTPException(status_code=404, detail="patient not found")

    request = AuthorizationRequest(
        actor_id=x_actor_id or "",
        patient_id=patient_id,
        action=Action.VIEW_PATIENT_CHART,
        purpose=purpose_enum,
        correlation_id=x_request_id,
    )

    try:
        result = run_patient_view(
            request,
            authorizer=_STAFF_ACCESS_GATE,
            repository=SqlChartRepository(db),
        )
    except AuthorizationDenied as denial:
        _write_audit(
            db,
            actor=x_actor_id or "unknown",
            message=(
                f"patient_view outcome=denied patient_id={patient_id} "
                f"reason={denial.denial.reason.value} correlation_id={denial.denial.correlation_id}"
            ),
        )
        raise HTTPException(
            status_code=403,
            detail={"reason": denial.denial.reason.value, "correlation_id": denial.denial.correlation_id},
        )

    _write_audit(
        db,
        actor=x_actor_id or "unknown",
        message=(
            f"patient_view outcome={result.outcome.value} patient_id={patient_id} "
            f"evidence_count={len(result.evidence_ids)} correlation_id={result.correlation_id} "
            f"reasons={','.join(r.value for r in result.reasons)}"
        ),
    )
    return result


@app.get("/records/search", response_model=list[RecordSearchHit])
def search_records(
    q: str = Query(..., min_length=1, description="free-text query"),
    db: Session = Depends(get_db),
):
    """
    Free-text search across records.

    DEBT D8: full-table ILIKE scan on records.body with NO supporting index and
    NO result limit. On a real chart corpus this scans every row every call.
    """
    try:
        # full-table scan on body — no index, no limit (deliberate debt)
        rows = (
            db.execute(
                select(Record).where(Record.body.ilike(f"%{q}%"))
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError:
        log.exception("search_records: database error")
        raise HTTPException(status_code=503, detail="database unavailable")

    return [RecordSearchHit.model_validate(r) for r in rows]
