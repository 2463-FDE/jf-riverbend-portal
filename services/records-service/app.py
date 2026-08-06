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
from contextlib import asynccontextmanager
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
    PatientViewOutcome,
    PatientViewResult,
    Purpose,
    StaffAccessGate,
    ViewReason,
    run_patient_view,
)
from logging_config import configure
from models import AuditLog, Encounter, Patient, Record
from patient_view_repository import SqlChartRepository
from reconciliation import build_reconciliation_result
from schemas import (
    EncounterOut,
    EncounterWithRecords,
    PatientChart,
    PatientDetail,
    PatientPage,
    PatientSummary,
    ReconciliationResult,
    RecordOut,
    RecordSearchHit,
)

log = configure(settings.service_name)


def _internal_token_is_configured() -> bool:
    """Round-13 review (2026-08-06, PR #20): the same presence/length floor
    _verify_internal_token enforces on every request, checked once here so
    /healthz can fail before a misconfigured deploy ever serves a real
    request — see gateway's and intake-service's identical fix."""
    configured = settings.internal_service_token
    return bool(configured) and len(configured) >= _MIN_INTERNAL_TOKEN_LENGTH


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Round-17 review (2026-08-06): failing only at /healthz (round-13)
    means a misconfigured container sits "unhealthy" until the healthcheck's
    retry budget expires, with the actual cause buried behind a generic
    failure. This fails at process startup instead: uvicorn logs the exact
    RuntimeError below and exits non-zero immediately, so
    `docker compose ps`/`logs` shows "Exited" with a clear message, not a
    slow unhealthy churn. Verified this does not fire under this repo's
    TestClient(app).get(...) pattern (no `with` block — Starlette only runs
    lifespan startup/shutdown for a context-managed TestClient), so it
    cannot break any existing test; it only ever runs for a real
    uvicorn-started process. See gateway's and intake-service's identical
    fix."""
    if not _internal_token_is_configured():
        raise RuntimeError(
            f"INTERNAL_SERVICE_TOKEN is not set (or is shorter than "
            f"{_MIN_INTERNAL_TOKEN_LENGTH} chars) — refusing to start. Set a real "
            f"random value (e.g. `openssl rand -hex 32`) in .env; see .env.example."
        )
    yield


app = FastAPI(title="Riverbend records-service", lifespan=lifespan)

_STAFF_ACCESS_GATE = StaffAccessGate()


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


def _deny_patient_view(
    db: Session,
    x_actor_id: Optional[str],
    patient_id: int,
    denial: "AuthorizationDenied",
    *,
    action: str = "patient_view",
) -> None:
    """Shared by every place a route calls into StaffAccessGate (round-19
    review, 2026-08-06: get_patient_view's standalone pre-check and
    run_patient_view's own internal one; Stage 2 (Week 6): also
    get_patient_reconciliation) — so a denial is audited and rejected
    identically regardless of which call raised it. `action` labels the
    audit message with the route that was denied (default "patient_view"
    keeps every existing call site/test unchanged); always raises, never
    returns."""
    _write_audit(
        db,
        actor=x_actor_id or "unknown",
        message=(
            f"{action} outcome=denied patient_id={patient_id} "
            f"reason={denial.denial.reason.value} correlation_id={denial.denial.correlation_id}"
        ),
    )
    raise HTTPException(
        status_code=403,
        detail={"reason": denial.denial.reason.value, "correlation_id": denial.denial.correlation_id},
    )


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

    request = AuthorizationRequest(
        actor_id=x_actor_id or "",
        patient_id=patient_id,
        action=Action.VIEW_PATIENT_CHART,
        purpose=purpose_enum,
        correlation_id=x_request_id,
    )

    # Round-19 review (2026-08-06): authorization now runs before any patient
    # lookup. Round-15's fix put the existence check first, which let a
    # caller holding the internal token but no valid actor get 404 for a
    # nonexistent patient_id and 403 for an existing one — a patient-ID
    # existence oracle for exactly the callers StaffAccessGate exists to
    # keep at zero reads. Calling authorize() directly here (the same
    # AuthorizationPort run_patient_view uses internally) denies before any
    # db.get(Patient, ...) runs, so a denied actor now gets an identical 403
    # regardless of whether patient_id exists.
    try:
        _STAFF_ACCESS_GATE.authorize(request)
    except AuthorizationDenied as denial:
        _deny_patient_view(db, x_actor_id, patient_id, denial)

    # Round-15 review (2026-08-06): SqlChartRepository's first read only
    # loads encounters for patient_id — an unknown id returns an empty
    # result set, not an error, so run_patient_view would previously produce
    # a normal "no evidence" COMPLETED/escalated result for a patient that
    # does not exist at all. A typo'd or stale patient_id then looked
    # identical to a real patient with an empty chart, which is a clinical
    # safety problem for a chart summary a clinician is meant to trust.
    # Checked here, same 404 idiom get_patient already uses above, now after
    # authorization (round-19) but still before any chart read runs. No
    # audit_logs row is written for a nonexistent id — same reasoning as
    # _verify_internal_token's own rejection path: this is a not-found
    # response, not a chart access.
    try:
        patient_exists = db.get(Patient, patient_id) is not None
    except SQLAlchemyError:
        log.exception("get_patient_view: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")
    if not patient_exists:
        raise HTTPException(status_code=404, detail="patient not found")

    # run_patient_view re-authorizes internally — its own contract runs
    # authorize() exactly once before either read specialist, regardless of
    # the pre-check above. Given the identical `request`, StaffAccessGate is
    # a deterministic function of its inputs, so this can only ever ALLOW
    # here; kept fenced against AuthorizationDenied anyway rather than
    # assuming that invariant can never change.
    try:
        result = run_patient_view(
            request,
            authorizer=_STAFF_ACCESS_GATE,
            repository=SqlChartRepository(db),
        )
    except AuthorizationDenied as denial:
        _deny_patient_view(db, x_actor_id, patient_id, denial)

    _write_audit(
        db,
        actor=x_actor_id or "unknown",
        message=(
            f"patient_view outcome={result.outcome.value} patient_id={patient_id} "
            f"evidence_count={len(result.evidence_ids)} correlation_id={result.correlation_id} "
            f"reasons={','.join(r.value for r in result.reasons)}"
        ),
    )

    # Round-19 review: a repository/specialist exception during the chart
    # read is caught inside run_patient_view itself and surfaces as
    # outcome=ESCALATED with reasons=[NODE_FAILURE] (see
    # libs/patient_view_agent/runtime.py::node_failure_result) — not an
    # exception this route can catch. Without this check, a database error,
    # schema drift, or any other backend read failure returned a normal 200
    # patient-view result, indistinguishable from a real (if unhelpful)
    # clinical answer to callers and uptime monitors. The audit row above is
    # still written either way; `result.summary` is already a safe,
    # non-PHI, pre-templated string for exactly this case.
    if result.outcome == PatientViewOutcome.ESCALATED and ViewReason.NODE_FAILURE in result.reasons:
        raise HTTPException(status_code=503, detail=result.summary)

    return result


@app.get("/patients/{patient_id}/reconciliation", response_model=ReconciliationResult)
def get_patient_reconciliation(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """
    Stage 2 (Week 6) — read-only "possible duplicate patient" reconciliation
    view (see reconciliation.py). Same two-layer trust as get_patient_view
    above: (1) _verify_internal_token proves this call came from the gateway;
    (2) StaffAccessGate is authenticated-staff access, NOT patient-specific
    authorization — this route does not fix RIV-201 either. Reuses
    Action.VIEW_PATIENT_CHART (no dedicated Action exists for this, and
    adding one would mean editing libs/patient_view_agent's shared contract,
    out of scope here) and always requests Purpose.TREATMENT — there is no
    legitimate non-treatment purpose for this view in this slice, so unlike
    get_patient_view it takes no `purpose` query param.

    Never returns a raw ssn; matches are exact-SSN-only (reconciliation.py);
    every read is audited, success or denial, same as get_patient_view.
    """
    _verify_internal_token(x_internal_token)

    request = AuthorizationRequest(
        actor_id=x_actor_id or "",
        patient_id=patient_id,
        action=Action.VIEW_PATIENT_CHART,
        purpose=Purpose.TREATMENT,
        correlation_id=x_request_id,
    )

    # Same anti-oracle ordering as get_patient_view (round-19): authorization
    # before any patient lookup, so a denied actor gets an identical 403
    # regardless of whether patient_id exists.
    try:
        scope = _STAFF_ACCESS_GATE.authorize(request)
    except AuthorizationDenied as denial:
        _deny_patient_view(db, x_actor_id, patient_id, denial, action="reconciliation")

    try:
        patient = db.get(Patient, patient_id)
    except SQLAlchemyError:
        log.exception("get_patient_reconciliation: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    try:
        result = build_reconciliation_result(db, patient_id, patient, scope.correlation_id)
    except SQLAlchemyError:
        log.exception("get_patient_reconciliation: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    _write_audit(
        db,
        actor=x_actor_id or "unknown",
        message=(
            f"reconciliation outcome=completed patient_id={patient_id} "
            f"match_count={len(result.source_records) - 1} "
            f"discrepancy_count={len(result.discrepancies)} correlation_id={result.correlation_id}"
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
