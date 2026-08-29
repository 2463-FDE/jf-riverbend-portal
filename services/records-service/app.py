"""
records-service — patient + records read façade (FHIR-ish).

Serves patient demographics and a patient's encounters/records to the portal.

Stage 3 addition: `GET /patients/{id}/view`, a new, additive route that wires
the Week 4/5 `libs.patient_view_agent` supervisor to real data, backed by
`SqlChartRepository` instead of that library's fixture `SeededChartRepository`.

Week 4 catch-up: `get_patient`, `get_patient_records`, and `get_patient_view`
are now all gated by `SqlPatientAccessGate` (patient_access_gate.py) — a
real, database-backed per-(actor, patient) authorization check against
`patient_access_grants` (migration 014), replacing the earlier
authenticated-staff-only `StaffAccessGate` that this route used to use
(`StaffAccessGate` itself is untouched in `libs.patient_view_agent` for
tests/rollback — it is simply no longer wired into any production route).
This closes DEBT D11 / RIV-201 — see docs/analysis/RIV-201-patient-records-IDOR.md
for the gap this replaces and patient_access_gate.py for the new check.
"""
import hmac
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import exists, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import roles_config
from config import settings
from db import get_db, get_sessionmaker
from libs.patient_view_agent import (
    Action,
    AuthorizationDenied,
    AuthorizationRequest,
    PatientViewOutcome,
    PatientViewResult,
    Purpose,
    ViewReason,
    run_patient_view,
)
from logging_config import configure
from models import (
    AgentDraftProvenance,
    AuditLog,
    Encounter,
    Patient,
    PatientSummaryReview,
    Record,
    User,
)
from patient_access_gate import (
    SqlPatientAccessGate,
    active_patient_ids_query,
    parse_user_id,
)
import agent_drafts
import agent_lifecycle
import messaging
import patient_summary
import policy_navigator_path
import review_queue
import summary_agent_path
from libs.agent_provenance import ProvenanceLabel, TraceRecorder
from libs.phi_crypto import PhiCryptoError
from libs.tracing.spans import new_correlation_id
from patient_view_repository import SqlChartRepository
from phi import decrypt_draft_text, decrypt_patient_field, get_key_provider
from reconciliation import build_reconciliation_result
from schemas import (
    AgentDraftCitationOut,
    AgentDraftDecisionRequest,
    AgentDraftOut,
    AgentSummaryOut,
    AgentSummaryRequestOut,
    EncounterOut,
    EncounterWithRecords,
    PatientChart,
    PatientDetail,
    PatientPage,
    PatientSummary,
    OwnResultsSummary,
    PolicyAnswerOut,
    PolicyCitationOut,
    PolicyQuestionRequest,
    ReviewDecisionOut,
    ReviewDecisionRequest,
    ReviewQueueItem,
    ReviewQueuePage,
    SummaryChangeOut,
    SummaryItemOut,
    ReconciliationResult,
    RecordOut,
    RecordSearchHit,
    CreateThreadRequest,
    MessageOut,
    SendMessageRequest,
    ThreadDetailOut,
    ThreadPage,
    ThreadStatusRequest,
    ThreadSummaryOut,
)

log = configure(settings.service_name)


def _internal_token_is_configured() -> bool:
    """Round-13 review (2026-08-06, PR #20): the same presence/length floor
    _verify_internal_token enforces on every request, checked once here so
    /healthz can fail before a misconfigured deploy ever serves a real
    request — see gateway's and intake-service's identical fix."""
    configured = settings.internal_service_token
    return bool(configured) and len(configured) >= _MIN_INTERNAL_TOKEN_LENGTH


def _check_patient_grant_coverage() -> None:
    """Grant-rollout-lockout risk, round 6 (2026-08-08 — high): migration 014
    ships patient_access_grants empty, and SqlPatientAccessGate denies any
    patient with no active grant. Without this check, a normal
    apply.sh + restart against a database that already has patients can boot
    straight into a clinic-wide chart-access outage with no hard failure —
    round 5's warning-only version was exactly this: visible in logs, but
    nothing mechanically stopped the bad deploy.

    Environment-gated fail-hard, not a blanket one: round 4 already
    established that hard-failing this unconditionally breaks the committed
    seed (255 patients, 7 grants) and any deliberately partial Phase-1
    rollout — `make up`/`make seed` must keep booting in dev. So this WARNS
    in every environment except `production` (ENVIRONMENT=production, unset
    elsewhere including this repo's own .env), and in production it RAISES —
    uvicorn then exits non-zero at startup instead of serving traffic that
    would deny every existing chart, mirroring the INTERNAL_SERVICE_TOKEN
    check above. Same underlying query as check_grant_coverage.sh, so the
    on-demand script and this always-on check can never disagree.

    Still no enforcement bypass: this only ever WARNS or RAISES, never
    disables SqlPatientAccessGate — this codebase's own safety rules for this
    authorization work explicitly rule out an all-staff or administrator
    bypass, so there is no flag that makes enforcement itself optional.

    Round 6 review (2026-08-08 — high, no-ship): the query itself can fail —
    migration 014 not applied yet (patient_access_grants/users missing), or
    the database simply unreachable at startup. A prior version swallowed
    that unconditionally, which meant the exact failure modes this guard
    exists to catch (schema drift, DB outage) let production boot HEALTHY
    with coverage never verified — chart routes would then fail at request
    time (503/403) instead of the deploy stopping up front. In production,
    "the check itself couldn't run" is now treated the same as "the check
    ran and found a gap": both refuse to start. Outside production it still
    only logs and continues — a query failure during ordinary compose
    startup ordering (DB not ready yet) must not turn `make up` into a crash
    loop. Never includes str(exc) in the raised message (only the exception
    type) — a DB-driver error string can embed the connection URL/password."""
    try:
        db = get_sessionmaker()()
        try:
            unreachable = db.execute(
                text(
                    """
                    SELECT count(*) FROM patients p WHERE NOT EXISTS (
                        SELECT 1 FROM patient_access_grants g JOIN users u ON u.id = g.user_id
                        WHERE g.patient_id = p.id
                          AND g.revoked_at IS NULL
                          AND (g.expires_at IS NULL OR g.expires_at > now())
                          AND u.is_active
                    )
                    """
                )
            ).scalar_one()
        finally:
            db.close()
    except SQLAlchemyError as e:
        log.error("startup grant-coverage check failed error_type=%s", type(e).__name__)
        if settings.environment == "production":
            raise RuntimeError(
                "refusing to start: could not verify patient grant coverage "
                f"(coverage query failed: {type(e).__name__} — migration 014 may "
                "not be applied, or the database is unreachable)"
            ) from e
        log.warning("startup grant-coverage check failed outside production — continuing")
        return
    if not unreachable:
        return
    message = (
        f"{unreachable} patient(s) have no active grant to an active user — those "
        f"charts will be denied until grants are backfilled (docs/runbook.md "
        f"'Phase 2', or run db/migrations/scripts/check_grant_coverage.sh)"
    )
    if settings.environment == "production":
        raise RuntimeError(f"refusing to start: {message}")
    log.warning("startup: %s", message)


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
    fix.

    Also runs the grant-coverage check above — same "real process only, never
    fires in this repo's unit tests" property; it raises (refusing to start)
    in production, warns everywhere else."""
    if not _internal_token_is_configured():
        raise RuntimeError(
            f"INTERNAL_SERVICE_TOKEN is not set (or is shorter than "
            f"{_MIN_INTERNAL_TOKEN_LENGTH} chars) — refusing to start. Set a real "
            f"random value (e.g. `openssl rand -hex 32`) in .env; see .env.example."
        )
    try:
        roles_config.reload()
        if not roles_config.roles():
            raise ValueError("no roles defined")
    except Exception as e:
        raise RuntimeError(
            f"could not load the RBAC config from {roles_config.config_path()!r} "
            f"({type(e).__name__}: {e}) — refusing to start, because every "
            f"authorized route would deny. Check that config/roles.yaml is present "
            f"in the image (see this service's Dockerfile) or set ROLES_CONFIG_PATH."
        )
    _check_patient_grant_coverage()
    # w8-planner-2 P2 (adr/0012): this service decrypts ssn/dob/notes on
    # every patient read. Same fail-at-startup discipline as
    # INTERNAL_SERVICE_TOKEN/roles.yaml above, not a per-request check —
    # see services/intake-service/app.py's identical lifespan addition.
    try:
        get_key_provider()
    except PhiCryptoError as e:
        raise RuntimeError(f"PHI key configuration is invalid — refusing to start: {e}")
    yield


app = FastAPI(title="Riverbend records-service", lifespan=lifespan)


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
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """Paginated patient roster / name search, scoped to the caller's grants.

    PR #23 review round 2 (2026-08-07, finding 2): this route previously
    returned every patient's id/name/DOB/gender/MRN to any authenticated staff
    account — an enumeration/IDOR path the internal-token check alone did not
    close (it only proves the gateway called, not that THIS user may see these
    patients). It is now patient-scoped like every other read: results are
    filtered in SQL to the caller's active grants (active_patient_ids_query),
    so a user only ever sees rows they hold a grant for. Front-desk lookup keeps
    working because trusted intake grants the registrar their new patient (see
    intake-service). The returned set is audited; a DB/policy error is a 503,
    never a silently empty 200.
    """
    _verify_internal_token(x_internal_token)
    actor = x_actor_name or x_actor_id or "unknown"
    user_id = parse_user_id(x_actor_id)
    if user_id is None:
        _write_audit(db, actor=actor, message="list_patients denied: no valid actor")
        return PatientPage(items=[], total=0, limit=limit, offset=offset)

    try:
        # PR #33 review [high]: this route filtered by active grant only and
        # never checked the caller's role, so an active user holding a grant
        # could list patient names, DOB, gender and MRN with an unknown or
        # downgraded role — a stale-role gap at the very boundary this branch
        # exists to close. Inside the try because this route reports a store
        # failure as 503 rather than an empty roster; a genuine denial raises
        # HTTPException, which is not a SQLAlchemyError, so it still surfaces
        # as 403.
        _authorize_actor_permission(
            db,
            x_actor_id=x_actor_id,
            x_actor_name=x_actor_name,
            required_permission="patients.read",
            audit_action="list_patients",
        )
        base = select(Patient).where(Patient.id.in_(active_patient_ids_query(user_id)))
        count_q = (
            select(func.count())
            .select_from(Patient)
            .where(Patient.id.in_(active_patient_ids_query(user_id)))
        )
        if q:
            pattern = f"%{q}%"
            base = base.where(Patient.name.ilike(pattern))
            count_q = count_q.where(Patient.name.ilike(pattern))

        total = db.execute(count_q).scalar_one()
        rows = (
            db.execute(base.order_by(Patient.id).limit(limit).offset(offset))
            .scalars()
            .all()
        )
    except SQLAlchemyError as exc:
        log.error("list_patients: database error error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")

    _write_audit(
        db,
        actor=actor,
        message=f"list_patients returned {len(rows)} patient(s): {sorted(p.id for p in rows)}",
    )
    return PatientPage(
        items=[_decrypted_patient_summary(p) for p in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _decrypted_patient_summary(p: Patient) -> PatientSummary:
    """w8-planner-2 P2 (adr/0012): PatientSummary.model_validate(p) alone
    would put dob's ciphertext envelope straight into the response — dob is
    decrypted here and overlaid via model_copy, never by mutating `p`
    itself (mutating an ORM attribute with plaintext risks a later
    session.commit() on this object writing plaintext back over the real
    encrypted column — see phi.py's docstring)."""
    summary = PatientSummary.model_validate(p)
    return summary.model_copy(update={"dob": decrypt_patient_field(p.id, "dob", p.dob, p.dob_key_version)})


def _actor_label(x_actor_name: Optional[str], x_actor_id: Optional[str]) -> str:
    """Human-legible audit actor: username (X-Actor-Name) when the gateway
    forwarded it, else the stable users.id (X-Actor-Id), else 'unknown'.
    Authorization always uses the id; this is display/audit only."""
    return x_actor_name or x_actor_id or "unknown"


def _redact_clinical_fields(db, detail, *, x_actor_id):
    """Withhold `patients.notes` from roles that may not read clinical notes.

    Client decision (2026-08-14): Front Desk and Billing do not read clinical
    notes, and this field is served under `patients.read`, which both hold. It
    is a single free-text column carrying both kinds of content — the seeded
    data has "PCN allergy noted at front desk." sitting beside "Prefers morning
    appts." — so there is no way to hand over the scheduling half without the
    allergy half.

    Fail closed: withhold the whole field unless the role may read clinical
    notes. Those roles lose the scheduling preferences too, which is a real
    cost the client accepted rather than leak allergy text to a role the signed
    grid excludes. Splitting the column into clinical and non-clinical notes is
    the correct end state and is a funded follow-up, not this cycle.

    Redacts rather than blanks silently: `notes_withheld` tells the caller the
    field exists and was withheld, so a UI can say so instead of implying the
    patient has no notes.
    """
    if _actor_may_read_clinical_notes(db, x_actor_id):
        return detail
    return detail.model_copy(update={"notes": None, "notes_withheld": True})


def _actor_may_read_clinical_notes(db, x_actor_id):
    """True when this actor's role holds records.read. Deliberately a separate,
    cheap lookup rather than threading the role down from the authorization
    check: get_patient authorizes on patients.read, so the role it verified is
    not the one this question needs."""
    user_id = parse_user_id(x_actor_id)
    if user_id is None:
        return False
    try:
        row = db.execute(
            select(User.role, User.is_active).where(User.id == user_id)
        ).one_or_none()
    except SQLAlchemyError:
        return False  # fail closed — an unreadable role withholds the field
    if row is None or not row.is_active:
        return False
    return "records.read" in roles_config.permissions_for(row.role)


def _authorize_actor_permission(
    db: Session,
    *,
    x_actor_id: Optional[str],
    x_actor_name: Optional[str] = None,
    required_permission: str,
    audit_action: str,
) -> None:
    """Enforce the signed permission matrix HERE, at the data boundary.

    The gateway's require_permission is the first layer, not the boundary:
    this service's port is published (docker-compose.yml), so a direct caller
    skips it entirely. The client's direction was explicit — permissions are
    enforced in the query path, not by hiding buttons.

    The role comes from `users.role` in the database, never from a header. A
    header would be spoofable by exactly the direct caller this exists to
    stop, and reading the row also revalidates `is_active` and `role` on every
    request — which closes the stale-session gap raised in PR #26 review: the
    gateway stamps the role into Redis once at login, so a downgraded or
    disabled account would otherwise keep its old role until the session
    lapsed (up to the 8h absolute cap). That matters most for the roster
    migration, where changing someone's role must take effect now, not after
    their next sign-in.

    Runs BEFORE the per-patient grant check and touches only the actor, so it
    creates no patient-existence oracle: the denial is identical whether or
    not the requested patient exists.
    """
    user_id = parse_user_id(x_actor_id)
    if user_id is None:
        # Not this function's call. An unparseable/absent actor is already
        # denied downstream by SqlPatientAccessGate with a structured reason
        # code and its own audit vocabulary; duplicating it here would replace
        # a specific denial with a vaguer one. This function judges permission,
        # not actor validity — so hand off and let the gate speak.
        return

    try:
        row = db.execute(
            select(User.role, User.is_active).where(User.id == user_id)
        ).one_or_none()
    except SQLAlchemyError as exc:
        # Deliberately re-raised rather than turned into a status here. This
        # service has TWO established contracts for a database failure and they
        # differ by route: the grant-gated patient routes deny closed with 403
        # (test_grant_lookup_failure_denies_closed), while the list and search
        # routes report 503 rather than look like a legitimately empty result
        # (test_search_db_failure_is_503_not_silently_empty). A single decision
        # made here would silently override one of them, so the caller decides.
        log.error("permission check: authorization store unreadable for actor error_type=%s", type(exc).__name__)
        raise

    if row is None or not row.is_active:
        _write_audit(
            db,
            actor=_actor_label(x_actor_name, x_actor_id),
            message=f"{audit_action} denied: actor unknown or inactive",
        )
        raise HTTPException(status_code=403, detail="not authorized")

    if required_permission not in roles_config.permissions_for(row.role):
        _write_audit(
            db,
            actor=_actor_label(x_actor_name, x_actor_id),
            message=(
                f"{audit_action} denied: role lacks {required_permission}"
            ),
        )
        raise HTTPException(status_code=403, detail="not authorized")


def _authorize_or_deny(
    db: Session,
    *,
    x_actor_id: Optional[str],
    x_actor_name: Optional[str] = None,
    x_request_id: Optional[str],
    patient_id: int,
    action: Action = Action.VIEW_PATIENT_CHART,
    purpose: Purpose = Purpose.TREATMENT,
    audit_action: str = "patient_access",
    required_permission: str = "records.read",
) -> None:
    """Shared by get_patient/get_patient_records/get_patient_view/
    get_patient_reconciliation — runs SqlPatientAccessGate.authorize()
    BEFORE any patient lookup (see that file's module docstring: this check
    never queries `patients`, so it creates no existence oracle) and turns
    a denial into an audited 403. `audit_action` labels the denial's audit
    message with the route that denied (distinct from the `Action` enum
    param above, which is the authorization request's own action type —
    default "patient_access" keeps every existing call site/test
    unchanged). Always raises on deny; returns normally on allow."""
    try:
        _authorize_actor_permission(
            db,
            x_actor_id=x_actor_id,
            x_actor_name=x_actor_name,
            required_permission=required_permission,
            audit_action=audit_action,
        )
    except SQLAlchemyError:
        # Fail closed, matching this route family's existing contract: an
        # authorization store we cannot read denies rather than reporting an
        # outage the caller might retry past.
        _write_audit(
            db,
            actor=_actor_label(x_actor_name, x_actor_id),
            message=f"{audit_action} denied: authorization store unreadable",
        )
        # Same 403 AND the same structured body the gate produces for an
        # unreadable policy store, so callers keep one denial contract rather
        # than two that differ by which query happened to fail first.
        raise HTTPException(
            status_code=403,
            detail={"reason": "policy_error", "correlation_id": x_request_id},
        )
    request = AuthorizationRequest(
        actor_id=x_actor_id or "",
        patient_id=patient_id,
        action=action,
        purpose=purpose,
        correlation_id=x_request_id,
    )
    try:
        SqlPatientAccessGate(db).authorize(request)
    except AuthorizationDenied as denial:
        _deny_patient_access(
            db, x_actor_id, patient_id, denial, x_actor_name=x_actor_name, action=audit_action
        )


@app.get("/patients/{patient_id}", response_model=PatientDetail)
def get_patient(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """Patient demographics, or 404.

    Week 4 catch-up: this was the original RIV-201 IDOR (DEBT D11) — any
    valid session could read any patient_id's demographics, including SSN
    and DOB. Now gated the same way /patients/{id}/view already was:
    _verify_internal_token proves the call came via the gateway, then a real
    per-(actor, patient) grant is checked before any patient row is read —
    see docs/analysis/RIV-201-patient-records-IDOR.md and
    patient_access_gate.py.
    """
    _verify_internal_token(x_internal_token)
    _authorize_or_deny(db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, x_request_id=x_request_id, patient_id=patient_id, required_permission="patients.read")

    try:
        patient = db.get(Patient, patient_id)
    except SQLAlchemyError as exc:
        log.error("get_patient: database error for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")

    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    _write_audit(
        db,
        actor=_actor_label(x_actor_name, x_actor_id),
        message=f"get_patient outcome=allowed patient_id={patient_id} correlation_id={x_request_id or ''}",
    )
    detail = _decrypted_patient_detail(patient)
    return _redact_clinical_fields(db, detail, x_actor_id=x_actor_id)


def _decrypted_patient_detail(p: Patient) -> PatientDetail:
    """w8-planner-2 P2 (adr/0012): same reasoning as _decrypted_patient_summary
    — decrypt dob/ssn/notes and overlay via model_copy, never by mutating
    the ORM object `p` itself."""
    detail = PatientDetail.model_validate(p)
    return detail.model_copy(
        update={
            "dob": decrypt_patient_field(p.id, "dob", p.dob, p.dob_key_version),
            "ssn": decrypt_patient_field(p.id, "ssn", p.ssn, p.ssn_key_version),
            "notes": decrypt_patient_field(p.id, "notes", p.notes, p.notes_key_version),
        }
    )


@app.get("/patients/{patient_id}/records", response_model=PatientChart)
def get_patient_records(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """
    Assemble a patient's full chart: their encounters and, per encounter, its records.

    Week 4 catch-up: DEBT D11 (IDOR) is fixed the same way get_patient above
    is — real per-(actor, patient) authorization runs before any encounter/
    record query. DEBT D8 (N+1) is untouched and deliberate (see comment
    below); this fix is scoped to authorization only.
    """
    _verify_internal_token(x_internal_token)
    _authorize_or_deny(db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, x_request_id=x_request_id, patient_id=patient_id)

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
    except SQLAlchemyError as exc:
        log.error("get_patient_records: database error for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")

    _write_audit(
        db,
        actor=_actor_label(x_actor_name, x_actor_id),
        message=f"get_patient_records outcome=allowed patient_id={patient_id} correlation_id={x_request_id or ''}",
    )
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
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("patient_view: failed to write audit_logs entry error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")


def _deny_patient_access(
    db: Session,
    x_actor_id: Optional[str],
    patient_id: int,
    denial: "AuthorizationDenied",
    *,
    x_actor_name: Optional[str] = None,
    action: str = "patient_access",
) -> None:
    """Week 4 catch-up: renamed from _deny_patient_view — shared by every
    route that calls SqlPatientAccessGate.authorize() (get_patient,
    get_patient_records, get_patient_view, get_patient_view's own internal
    run_patient_view re-check, and get_patient_reconciliation) so a denial
    is audited and rejected identically regardless of which route or call
    site raised it. `action` labels the audit message with the route that
    was denied (default "patient_access" keeps every existing call site/
    test unchanged); always raises, never returns."""
    _write_audit(
        db,
        actor=_actor_label(x_actor_name, x_actor_id),
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
    a grant-table lookup keyed on that spoofed value would run — unauthenticated
    chart access for any patient_id that happened to have a grant on file,
    plus an audit_logs row attributed to a spoofed actor.

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
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """
    Stage 3 — bounded, evidence-cited patient-chart view via
    `libs.patient_view_agent.run_patient_view`, backed by real data
    (`SqlChartRepository`) and gated by `SqlPatientAccessGate`.

    Two layers of trust, in order: (1) `_verify_internal_token` proves this
    call actually came from the gateway (not a direct caller hitting this
    service's published host port with a spoofed `X-Actor-Id`); (2)
    `SqlPatientAccessGate` (Week 4 catch-up — replaces the earlier
    authenticated-staff-only `StaffAccessGate`) checks a real per-(actor,
    patient) grant in `patient_access_grants` before any patient lookup —
    see docs/analysis/RIV-201-patient-records-IDOR.md and
    patient_access_gate.py. This route, `get_patient`, and
    `get_patient_records` now share the exact same authorization boundary.
    """
    _verify_internal_token(x_internal_token)

    try:
        purpose_enum = Purpose(purpose)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid purpose '{purpose}'")

    try:
        _authorize_actor_permission(
            db,
            x_actor_id=x_actor_id,
            x_actor_name=x_actor_name,
            required_permission="records.read",
            audit_action="patient_view",
        )
    except SQLAlchemyError:
        _write_audit(
            db,
            actor=_actor_label(x_actor_name, x_actor_id),
            message="patient_view denied: authorization store unreadable",
        )
        raise HTTPException(
            status_code=403,
            detail={"reason": "policy_error", "correlation_id": x_request_id},
        )
    request = AuthorizationRequest(
        actor_id=x_actor_id or "",
        patient_id=patient_id,
        action=Action.VIEW_PATIENT_CHART,
        purpose=purpose_enum,
        correlation_id=x_request_id,
    )
    access_gate = SqlPatientAccessGate(db)

    # Round-19 review (2026-08-06): authorization runs before any patient
    # lookup. Round-15's fix put the existence check first, which let a
    # caller holding the internal token but no valid actor get 404 for a
    # nonexistent patient_id and 403 for an existing one — a patient-ID
    # existence oracle for exactly the callers this gate exists to keep at
    # zero reads. Calling authorize() directly here (the same
    # AuthorizationPort run_patient_view uses internally, below) denies
    # before any db.get(Patient, ...) runs, so a denied actor now gets an
    # identical 403 regardless of whether patient_id exists.
    try:
        access_gate.authorize(request)
    except AuthorizationDenied as denial:
        _deny_patient_access(db, x_actor_id, patient_id, denial, x_actor_name=x_actor_name)

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
    except SQLAlchemyError as exc:
        log.error("get_patient_view: database error for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")
    if not patient_exists:
        raise HTTPException(status_code=404, detail="patient not found")

    # run_patient_view re-authorizes internally — its own contract runs
    # authorize() exactly once before either read specialist, regardless of
    # the pre-check above. Given the identical `request` and the same
    # access_gate instance, SqlPatientAccessGate is a deterministic function
    # of its inputs, so this can only ever ALLOW here; kept fenced against
    # AuthorizationDenied anyway rather than assuming that invariant can
    # never change.
    try:
        result = run_patient_view(
            request,
            authorizer=access_gate,
            repository=SqlChartRepository(db),
        )
    except AuthorizationDenied as denial:
        _deny_patient_access(db, x_actor_id, patient_id, denial, x_actor_name=x_actor_name)

    _write_audit(
        db,
        actor=_actor_label(x_actor_name, x_actor_id),
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


_SEARCH_RESULT_LIMIT = 50  # bound results (PR #23 review round 2, finding 4)


@app.get("/patients/{patient_id}/reconciliation", response_model=ReconciliationResult)
def get_patient_reconciliation(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """
    Stage 2 (Week 6) — read-only "possible duplicate patient" reconciliation
    view (see reconciliation.py).

    Week 4 catch-up (Codex review, 2026-08-07, PR #22 — high, no-ship): this
    route now shares the exact same authorization boundary as get_patient/
    get_patient_records/get_patient_view — SqlPatientAccessGate, not the
    earlier authenticated-staff-only StaffAccessGate. Two authorization
    steps, not one: (1) the REQUESTED patient_id is authorized before
    anything runs, same as every other route in this file; (2)
    build_reconciliation_result independently authorizes EVERY SSN-matched
    candidate patient_id before loading or returning any of its details
    (see authorized_patient_ids in patient_access_gate.py) — an unauthorized
    candidate is discarded completely: no id, name, DOB, allergies,
    medications, evidence, or count attributable to it ever reaches the
    response. This closes the cross-patient disclosure the review flagged
    (matched charts were previously returned on the strength of the
    requested patient's own authorization alone).

    Reuses Action.VIEW_PATIENT_CHART (no dedicated Action exists for this,
    and adding one would mean editing libs/patient_view_agent's shared
    contract, out of scope here) and always requests Purpose.TREATMENT —
    there is no legitimate non-treatment purpose for this view in this
    slice, so unlike get_patient_view it takes no `purpose` query param.

    Never returns a raw ssn; matches are exact-SSN-only (reconciliation.py);
    every read is audited, success or denial, same as get_patient_view.
    """
    _verify_internal_token(x_internal_token)
    _authorize_or_deny(
        db,
        x_actor_id=x_actor_id,
        x_actor_name=x_actor_name,
        x_request_id=x_request_id,
        patient_id=patient_id,
        audit_action="reconciliation",
    )

    try:
        patient = db.get(Patient, patient_id)
    except SQLAlchemyError as exc:
        log.error("get_patient_reconciliation: database error for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    try:
        result = build_reconciliation_result(
            db, patient_id, patient, x_request_id or "", actor_id=x_actor_id or ""
        )
    except SQLAlchemyError as exc:
        log.error("get_patient_reconciliation: database error for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")

    _write_audit(
        db,
        actor=_actor_label(x_actor_name, x_actor_id),
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
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """
    Free-text search across records, scoped in SQL to the caller's grants.

    PR #23 review round 2 (2026-08-07, finding 4): this route previously loaded
    every matching record BODY first, then filtered by grant in Python — and a
    grant-lookup failure returned a silently empty 200, hiding the error, with
    no audit. Now the active-grant set is joined INTO the query
    (active_patient_ids_query), so bodies for unauthorized patients are never
    read; a DB/policy error surfaces as 503 (not empty success); results are
    bounded; and the patients actually returned are audited. A hit for a patient
    the caller has no grant for is simply never selected.

    DEBT D8: still a full-table ILIKE scan on records.body with no supporting
    index — untouched, deliberate debt (this fix is scoped to authorization),
    now at least bounded by _SEARCH_RESULT_LIMIT.
    """
    _verify_internal_token(x_internal_token)
    actor = x_actor_name or x_actor_id or "unknown"
    user_id = parse_user_id(x_actor_id)
    if user_id is None:
        _write_audit(db, actor=actor, message="records_search denied: no valid actor")
        return []
    try:
        # Same placement reasoning as list_patients above.
        _authorize_actor_permission(
            db,
            x_actor_id=x_actor_id,
            x_actor_name=x_actor_name,
            required_permission="records.read",
            audit_action="records_search",
        )
    except SQLAlchemyError as exc:
        log.error("records_search: authorization store unreadable error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")

    try:
        rows = (
            db.execute(
                select(Record)
                .where(
                    Record.patient_id.in_(active_patient_ids_query(user_id)),
                    Record.body.ilike(f"%{q}%"),
                )
                .order_by(Record.id)
                .limit(_SEARCH_RESULT_LIMIT)
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError as exc:
        log.error("search_records: database error error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="database unavailable")

    _write_audit(
        db,
        actor=actor,
        message=f"records_search returned {len(rows)} hit(s) across patients {sorted({r.patient_id for r in rows})}",
    )
    return [RecordSearchHit.model_validate(r) for r in rows]


# --------------------------------------------------------------------------- #
# patient-facing summary (S2)
#
# Deterministic: this route renders quotes by copying stored text, and there is
# no model call anywhere beneath it. See patient_summary.py's module docstring
# for why the summariser agent is deliberately NOT on this path — its whole
# design rests on record bodies never entering it, and the client's content
# rules require quoting those bodies verbatim.
#
# Authorization is the same gate every other chart read uses. A patient holds
# exactly one patient_access_grants row (their own), so _authorize_or_deny
# scopes them without a single patient-specific branch — the client's point
# that this is one mechanism applied to a different principal.
# --------------------------------------------------------------------------- #
@app.get("/patients/{patient_id}/summary", response_model=OwnResultsSummary)
def get_patient_summary(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """A patient's own results, quoted rather than interpreted.

    Three outcomes per result (client, 2026-08-14): a single value quotes and
    may carry a change, a panel quotes but never carries a change, and prose
    from which no clean quote can be taken refuses. A panel is never withheld
    wholesale — over-refusing was called out by name.
    """
    _verify_internal_token(x_internal_token)
    # required_permission is own_record.read, NOT this helper's records.read
    # default. A patient holds only own_record.read, so accepting the default
    # here would deny every patient at the data layer — the gateway would let
    # them through and this service would 403 them, which is the same dead end
    # one layer down. The grant check below still runs unchanged, so holding
    # the permission is necessary and not sufficient: the patient must also
    # hold a grant for this chart, which is the one grant they have.
    _authorize_or_deny(
        db,
        x_actor_id=x_actor_id,
        x_actor_name=x_actor_name,
        x_request_id=x_request_id,
        patient_id=patient_id,
        required_permission="own_record.read",
        audit_action="own_results_access",
    )

    try:
        rows = (
            db.execute(
                select(Record)
                .where(Record.patient_id == patient_id)
                # Chronological, NOT by id. render_items computes each change
                # against the previous result of the same test, and the date
                # shown to the patient is created_at — so ordering by id would
                # make the two disagree the moment a record is backfilled. A
                # lab imported late carries an older created_at and a larger
                # id; ordered by id it would appear as the newest result and
                # invert the up/down arrow against a result that actually came
                # after it. That is a patient reading "improving" when their
                # values got worse, so chronology has to come from the same
                # column the date does. id only breaks ties (the seed writes
                # whole charts within one timestamp).
                .order_by(Record.created_at.asc(), Record.id.asc())
            )
            .scalars()
            .all()
        )
    except SQLAlchemyError as exc:
        log.error("patient summary: chart unreadable patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="records temporarily unavailable")

    # The clinician gate (S3). Content the renderer refuses is shown only when
    # a named clinician approved that specific record; everything else — no
    # review, pending, rejected — stays refused. Loading the approved set here
    # and passing it in keeps render_items pure and keeps the default closed.
    approved = review_queue.approved_record_ids(db, patient_id)
    rendered = patient_summary.render_items(rows, approved_record_ids=approved)

    # Anything still refused is queued for a clinician. This is what makes the
    # queue real rather than decorative: it contains exactly what patients were
    # not allowed to see. Idempotent, so a refresh queues nothing.
    try:
        review_queue.enqueue_refusals(
            db,
            patient_id,
            [(i.record_id, i.refusal_reason and "no clean quote") for i in rendered if i.refusal_reason],
        )
        db.commit()
    except SQLAlchemyError as exc:
        # Queueing is bookkeeping for staff; it must never take down a
        # patient's ability to read their own results.
        db.rollback()
        log.error("review queue: could not enqueue refusals patient_id=%s error_type=%s", patient_id, type(exc).__name__)

    items = [_to_summary_item_out(item) for item in rendered]

    _write_audit(
        db,
        actor=_actor_label(x_actor_name, x_actor_id),
        message=f"own_results rendered {len(items)} result(s) for patient {patient_id}",
    )
    return OwnResultsSummary(patient_id=patient_id, items=items)


def _to_summary_item_out(item) -> SummaryItemOut:
    """Serialize one rendered result.

    Mapping rather than reusing the dataclass directly keeps patient_summary.py
    free of any response model: the content rules are testable without FastAPI,
    and changing the wire format cannot quietly change what a patient is
    allowed to read.
    """
    change = item.change
    return SummaryItemOut(
        record_id=item.record_id,
        title=item.title,
        date=item.date,
        shape=item.shape.value,
        quote=item.quote,
        reference_range=item.reference_range,
        change=(
            SummaryChangeOut(
                direction=change.direction,
                delta=change.delta,
                unit=change.unit,
                from_value=change.from_value,
                from_record_id=change.from_record_id,
                from_date=change.from_date,
            )
            if change is not None
            else None
        ),
        refusal_reason=item.refusal_reason,
        released_by_review=item.released_by_review,
        source_record_ids=list(item.source_record_ids),
    )


# --------------------------------------------------------------------------- #
# clinician review queue (S3)
#
# Gated on summary_review.decide AND records.read.
#
# The release action gets its OWN permission rather than reusing records.write,
# and the reason is worth stating because the first version of this gate got it
# wrong twice. records.write does not imply clinical read authority: `lab`
# holds write WITHOUT read by an explicit client decision (config/roles.yaml),
# so gating on write alone handed a lab tech the full text of withheld clinical
# notes plus the power to release it. Requiring read as well fixed that — but
# the deprecated `staff` role holds BOTH, and every seeded account is still on
# it, so billing, ROI clerks and IT admin could all still release withheld
# clinical content to a patient.
#
# "The grid is right, the account migration is outstanding" is a fair
# description of RBAC in general, and the wrong call here specifically: this
# feature INTRODUCES the disclosure capability, so shipping it reachable by
# twelve non-clinical accounts and deferring containment to a signature-gated
# migration weeks away is not a trade worth making. summary_review.decide is
# held only by clinician and nursing_ma, so the gate is closed for every
# existing account until someone is deliberately given the role.
#
# records.read is required alongside it as defence in depth: a reviewer reads
# chart text on this screen, so the permission for reading charts should be
# present too, and it stays correct even if the new permission is granted
# somewhere careless later.
#
# These routes are staff-facing. A patient holds only own_record.read and
# therefore cannot reach either of them.
# --------------------------------------------------------------------------- #
_REVIEW_PERMISSIONS = ("summary_review.decide", "records.read")


def _authorize_reviewer(db, *, x_actor_id, x_actor_name, audit_action) -> int:
    """Require an identified actor AND every permission a review needs.

    Returns the parsed actor id, so callers cannot forget to establish one.

    The explicit actor check matters because _authorize_actor_permission
    RETURNS SUCCESSFULLY when X-Actor-Id is missing or unparsable — by design,
    since its usual callers are grant-gated routes where the access gate then
    produces a structured denial. These routes have no such gate behind them,
    so without this a call carrying a valid internal token and no actor would
    have listed withheld clinical text with no user accountable and no role
    ever evaluated: the disclosure the new permission exists to contain,
    reached through a different door.
    """
    actor_id = parse_user_id(x_actor_id)
    if actor_id is None:
        log.warning("review queue: refused a call with no identifiable actor")
        raise HTTPException(status_code=403, detail="not authorized")

    for permission in _REVIEW_PERMISSIONS:
        _authorize_actor_permission(
            db,
            x_actor_id=x_actor_id,
            x_actor_name=x_actor_name,
            required_permission=permission,
            audit_action=audit_action,
        )
    return actor_id
@app.get("/review-queue", response_model=ReviewQueuePage)
def get_review_queue(
    limit: int = Query(default=50, ge=1, le=200),
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """Cases waiting on a clinician, oldest first."""
    _verify_internal_token(x_internal_token)
    actor_id = _authorize_reviewer(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, audit_action="review_queue_read"
    )

    try:
        # Grant-scoped, like every other chart read here. The queue shows
        # withheld note text, so holding the role is necessary and not
        # sufficient — the reviewer must also be granted that patient.
        reviews = review_queue.pending_reviews(
            db, active_patient_ids_query(actor_id), limit=limit
        )
        records = {
            r.id: r
            for r in db.execute(
                select(Record).where(Record.id.in_([rv.record_id for rv in reviews] or [0]))
            ).scalars().all()
        }
    except SQLAlchemyError as exc:
        log.error("review queue: unreadable error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="review queue temporarily unavailable")

    items = []
    for rv in reviews:
        rec = records.get(rv.record_id)
        items.append(
            ReviewQueueItem(
                id=rv.id,
                patient_id=rv.patient_id,
                record_id=rv.record_id,
                state=rv.state,
                reason=rv.reason,
                created_at=rv.created_at.isoformat() if rv.created_at else None,
                record_title=rec.title if rec else None,
                record_kind=rec.kind if rec else None,
                record_body=rec.body if rec else None,
                record_date=(
                    rec.created_at.date().isoformat() if rec and rec.created_at else None
                ),
            )
        )

    _write_audit(
        db,
        actor=_actor_label(x_actor_name, x_actor_id),
        message=f"review_queue listed {len(items)} pending case(s)",
    )
    return ReviewQueuePage(items=items)


@app.post("/review-queue/{review_id}/decision", response_model=ReviewDecisionOut)
def decide_review(
    review_id: int,
    req: ReviewDecisionRequest,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """Approve or reject. This is the decision that changes what a patient sees.

    Approving releases the record's own words to the patient, verbatim.
    Rejecting leaves the refusal in place permanently — the record is not
    re-queued on the patient's next visit, or the decision would mean nothing.
    """
    _verify_internal_token(x_internal_token)
    actor_id = _authorize_reviewer(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, audit_action="review_queue_decide"
    )

    try:
        review = review_queue.decide(
            db,
            review_id=review_id,
            state=req.decision,
            actor_id=actor_id,
            authorized_patient_ids=active_patient_ids_query(actor_id),
            note=req.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("review queue: decision failed review_id=%s error_type=%s", review_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="could not record the decision")

    if review is None:
        # Already decided, or never existed. Same answer for both: a stale
        # screen should not be able to discover which. Under the previous
        # read-then-write claim this branch was also unreachable in a race —
        # both callers succeeded and the later one silently won.
        raise HTTPException(status_code=409, detail="this case is no longer pending")

    # The decision and its audit row commit together, once. A previous version
    # committed the decision first and then called _write_audit, which commits
    # separately and raises 503 on failure — so an audit failure returned an
    # error to a clinician whose release was already live and visible to the
    # patient, and the screen told them "Nothing has changed". Both land or
    # neither does.
    state, record_id, patient_id = review.state, review.record_id, review.patient_id
    try:
        db.add(
            AuditLog(
                actor=_actor_label(x_actor_name, x_actor_id),
                message=(
                    f"review {review.id} {state} for record {record_id} "
                    f"(patient {patient_id})"
                ),
            )
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("review queue: decision+audit commit failed review_id=%s error_type=%s", review_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="could not record the decision")

    return ReviewDecisionOut(
        id=review_id,
        record_id=record_id,
        state=state,
        patient_visible=(state == review_queue.APPROVED),
    )


# --------------------------------------------------------------------------- #
# Week 8 agent draft: generate -> clinician review -> approved-only display.
#
# One correlation id spans all three. Generation issues it and it is persisted
# on the draft, so review and display reuse `draft.correlation_id` rather than
# minting their own — two ids would turn the client's one eight-stage trace
# into two traces of four, which is exactly the thing the trace exists to be.
#
# The patient route below returns the approved version or nothing. There is no
# parameter, and no status branch, by which pending or rejected text reaches it:
# `agent_drafts.approved_draft` filters on `status == APPROVED` in SQL, so
# unapproved text is never loaded rather than loaded and then hidden.
# --------------------------------------------------------------------------- #


def _draft_out(db: Session, draft: AgentDraftProvenance) -> AgentDraftOut:
    # adr/0012 follow-up (migration 032): the ONE place generated_text is
    # decrypted — every route that returns draft text (clinician review,
    # patient summary) goes through this function, and only after its own
    # authorization check already passed (see each route below). Never
    # mutates `draft` itself — same reasoning as _decrypted_patient_detail:
    # a later session.commit() on this object must never risk writing
    # plaintext back over the real ciphertext.
    plaintext = decrypt_draft_text(
        draft.patient_id, draft.version, draft.generated_text, draft.generated_text_key_version
    )
    return AgentDraftOut(
        id=draft.id,
        patient_id=draft.patient_id,
        version=draft.version,
        status=draft.status,
        provenance_label=draft.provenance_label,
        model_id=draft.model_id,
        validation_code=draft.validation_code,
        generated_text=plaintext,
        citations=[
            AgentDraftCitationOut(
                source_id=c.source_id, source_version=c.source_version,
                citation_id=c.citation_id, category=c.category,
            )
            for c in agent_drafts.citations_for(db, draft.id)
        ],
    )


def _actor_role(db: Session, actor_id: Optional[int]) -> str:
    if actor_id is None:
        return "unknown"
    row = db.execute(select(User.role).where(User.id == actor_id)).scalar_one_or_none()
    return row or "unknown"


def _latest_draft(db: Session, patient_id: int) -> Optional[AgentDraftProvenance]:
    return db.execute(
        select(AgentDraftProvenance)
        .where(AgentDraftProvenance.patient_id == patient_id)
        .order_by(AgentDraftProvenance.version.desc())
        .limit(1)
    ).scalars().first()


@app.post("/patients/{patient_id}/agent-draft", response_model=AgentDraftOut, status_code=201)
def generate_agent_draft(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """Run the agent for this patient and persist the resulting version.

    Gated on `summary_review.decide` AND a grant for this patient: generating a
    draft is the first half of the review a clinician is about to perform, and
    nobody who may not decide should be able to put a case in front of someone
    who can.
    """
    _verify_internal_token(x_internal_token)
    _authorize_or_deny(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, x_request_id=x_request_id,
        patient_id=patient_id, required_permission="summary_review.decide",
        audit_action="agent_draft_generate",
    )
    actor_id = parse_user_id(x_actor_id)
    correlation_id = x_request_id or new_correlation_id()

    try:
        outcome = summary_agent_path.generate_draft(
            db, patient_id=patient_id, actor_role=_actor_role(db, actor_id),
            correlation_id=correlation_id,
        )
        _write_audit(
            db, actor=_actor_label(x_actor_name, x_actor_id),
            message=(f"agent_draft_generate patient_id={patient_id} "
                     f"version={outcome.draft.version} label={outcome.label} "
                     f"passed={outcome.accepted} correlation_id={correlation_id}"),
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("agent draft: generation failed for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="could not generate a draft right now")
    return _draft_out(db, outcome.draft)


@app.get("/patients/{patient_id}/agent-draft", response_model=AgentDraftOut)
def get_agent_draft(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """The latest draft, whatever its status — the clinician-only view.

    This is the ONE route that may return unapproved draft text, and it is
    gated on the decide permission plus a grant. The patient route is a
    different function returning a different model, so there is no shared code
    path along which pending text could reach a patient.
    """
    _verify_internal_token(x_internal_token)
    _authorize_or_deny(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, x_request_id=x_request_id,
        patient_id=patient_id, required_permission="summary_review.decide",
        audit_action="agent_draft_read",
    )
    draft = _latest_draft(db, patient_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="no draft for this patient")
    return _draft_out(db, draft)


@app.post("/agent-drafts/{draft_id}/decision", response_model=AgentDraftOut)
def decide_agent_draft(
    draft_id: int,
    req: AgentDraftDecisionRequest,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """Approve or reject a version. This is what changes what a patient sees."""
    _verify_internal_token(x_internal_token)
    actor_id = _authorize_reviewer(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, audit_action="agent_draft_decide"
    )
    if req.decision not in (agent_drafts.APPROVED, agent_drafts.REJECTED):
        raise HTTPException(status_code=400, detail="decision must be approved or rejected")

    # Grant-scoped IN THE QUERY: a draft for a patient this reviewer does not
    # hold is never loaded, so it cannot be decided and its existence is not
    # disclosed by a distinguishable error.
    draft = db.execute(
        select(AgentDraftProvenance).where(
            AgentDraftProvenance.id == draft_id,
            AgentDraftProvenance.patient_id.in_(active_patient_ids_query(actor_id)),
        )
    ).scalars().one_or_none()
    if draft is None:
        raise HTTPException(status_code=404, detail="no such draft")

    try:
        review_trace = TraceRecorder(draft.correlation_id)  # the draft's own id, never a new one
        agent_drafts.decide(
            db, draft, approve=req.decision == agent_drafts.APPROVED, reviewed_by=actor_id,
            trace=review_trace,
        )
        # W10 Final Stage 4: append the review stage to the same durable
        # lifecycle stream generation already wrote to — same transaction
        # as the decision and audit rows below.
        agent_lifecycle.persist(db, draft.correlation_id, review_trace.events)
        _write_audit(
            db, actor=_actor_label(x_actor_name, x_actor_id),
            message=(f"agent_draft_decide draft_id={draft.id} version={draft.version} "
                     f"decision={req.decision} correlation_id={draft.correlation_id}"),
        )
        db.commit()
    except agent_drafts.DraftError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("agent draft: decision failed draft_id=%s error_type=%s", draft_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="could not record the decision")
    return _draft_out(db, draft)


@app.get("/patients/{patient_id}/agent-summary", response_model=AgentSummaryOut)
def get_agent_summary(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """The patient's own approved summary, or `available=false`.

    Gated on `own_record.read` — the permission only the `patient` role holds —
    plus the grant, exactly like `/patients/{id}/summary`. A pending version 2
    changes nothing here: `approved_draft` returns the one APPROVED row, so an
    approved version 1 keeps displaying until a later version is itself
    approved and supersedes it.
    """
    _verify_internal_token(x_internal_token)
    _authorize_or_deny(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, x_request_id=x_request_id,
        patient_id=patient_id, required_permission="own_record.read",
        audit_action="agent_summary_access",
    )
    try:
        draft = agent_drafts.approved_draft(db, patient_id)
        if draft is None:
            status = "pending" if agent_drafts.has_pending_draft(db, patient_id) else "none"
            return AgentSummaryOut(available=False, patient_id=patient_id, status=status)
        # Display is the eighth stage, recorded under the draft's OWN
        # correlation id — which is only knowable after the row is read, so the
        # stage is emitted here rather than passed into the read above.
        display_trace = TraceRecorder(draft.correlation_id)
        display_trace.display(
            draft_version=draft.version, label=ProvenanceLabel(draft.provenance_label),
        )
        # W10 Final Stage 4: append to the same durable lifecycle stream
        # generation/review already wrote to. This route was previously
        # read-only; it now commits this one append.
        agent_lifecycle.persist(db, draft.correlation_id, display_trace.events)
        db.commit()
        detail = _draft_out(db, draft)
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("agent summary: unreadable for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="temporarily unavailable")

    return AgentSummaryOut(
        available=True, patient_id=patient_id, status="approved", version=detail.version,
        provenance_label=detail.provenance_label,
        generated_text=detail.generated_text, citations=detail.citations,
    )


@app.post("/patients/{patient_id}/agent-summary/request",
          response_model=AgentSummaryRequestOut, status_code=201)
def request_agent_summary(
    patient_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """A patient asks for a summary of their own chart.

    The demo's first move is the patient asking, so generation cannot be
    clinician-only — but a patient initiating it must not become a patient
    reading it. This returns a RECEIPT: version, status, label, citations and
    the correlation id, and no draft text at all. `AgentSummaryRequestOut` has
    no text field, so there is no branch here that could be made to emit one.

    Gated on `own_record.read` — the permission only the `patient` role holds —
    plus the grant, which a patient holds for exactly one chart. The gateway
    resolves the id from the session rather than the request, so the browser
    never names a patient; this check is what makes that hold even if something
    upstream ever did.
    """
    _verify_internal_token(x_internal_token)
    _authorize_or_deny(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, x_request_id=x_request_id,
        patient_id=patient_id, required_permission="own_record.read",
        audit_action="agent_summary_request",
    )
    actor_id = parse_user_id(x_actor_id)
    correlation_id = x_request_id or new_correlation_id()

    try:
        outcome = summary_agent_path.generate_draft(
            db, patient_id=patient_id, actor_role=_actor_role(db, actor_id),
            correlation_id=correlation_id,
        )
        _write_audit(
            db, actor=_actor_label(x_actor_name, x_actor_id),
            message=(f"agent_summary_request patient_id={patient_id} "
                     f"version={outcome.draft.version} label={outcome.label} "
                     f"passed={outcome.accepted} correlation_id={correlation_id}"),
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("agent summary request: generation failed patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="could not request a summary right now")

    detail = _draft_out(db, outcome.draft)
    return AgentSummaryRequestOut(
        patient_id=patient_id, version=detail.version, status=detail.status,
        provenance_label=detail.provenance_label, correlation_id=outcome.draft.correlation_id,
        citations=detail.citations,
    )


# --------------------------------------------------------------------------- #
# W9.2 — secure patient-clinician messaging.
#
# One set of routes serves both audiences, deliberately: `messages.read`/
# `messages.write` are held by the `patient` role and by `clinician`/
# `nursing_ma` alike (config/roles.yaml), and a patient's own
# patient_access_grants row scopes them to exactly their own patient_id the
# same way a clinician's grants scope them to theirs — there is no second
# mechanism to keep in sync. The gateway is where the two audiences get
# different-looking paths (/patient/me/... vs /threads); down here it is one
# grant-scoped listing and one grant-scoped thread lookup.
#
# Thread creation is the one asymmetric action (only a patient starts a new
# thread, per the client's UX — a clinician replies to one, closes it,
# reopens it, but does not compose the first message), so it alone takes an
# explicit patient_id in the path rather than deriving one from an existing
# row.
# --------------------------------------------------------------------------- #

_MESSAGE_SUBJECT_MAX = 200
_MESSAGE_BODY_MAX = 4000


def _authorize_messaging(db, *, x_actor_id, x_actor_name, required_permission, audit_action) -> int:
    """Same shape as _authorize_reviewer: require an identified actor AND the
    role permission, for the routes with no patient-scoped grant gate ahead
    of them (the grant check for these is embedded in the thread/listing
    query itself — see thread_for_actor and thread_summaries)."""
    actor_id = parse_user_id(x_actor_id)
    if actor_id is None:
        log.warning("messaging: refused a call with no identifiable actor")
        raise HTTPException(status_code=403, detail="not authorized")
    _authorize_actor_permission(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name,
        required_permission=required_permission, audit_action=audit_action,
    )
    return actor_id


def _clean_text(value: str, *, max_len: int, field: str) -> str:
    cleaned = (value or "").strip()
    if not (1 <= len(cleaned) <= max_len):
        raise HTTPException(
            status_code=400, detail=f"{field} must be 1-{max_len} characters"
        )
    return cleaned


@app.get("/threads", response_model=ThreadPage)
def list_threads(
    limit: int = Query(default=50, ge=1, le=200),
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """The inbox — every thread for every patient this actor holds an active
    grant for. For a patient account that is exactly their own thread(s),
    via the same self-grant own_record.read already relies on; for a
    clinician it is every patient they are currently granted."""
    _verify_internal_token(x_internal_token)
    actor_id = _authorize_messaging(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name,
        required_permission="messages.read", audit_action="messages_inbox_read",
    )
    try:
        items = messaging.thread_summaries(
            db, patient_ids=active_patient_ids_query(actor_id), viewer_user_id=actor_id, limit=limit,
        )
    except SQLAlchemyError as exc:
        log.error("messaging: inbox unreadable for actor_id=%s error_type=%s", actor_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="messages temporarily unavailable")
    return ThreadPage(items=[ThreadSummaryOut(**i) for i in items])


@app.post("/patients/{patient_id}/threads", response_model=ThreadDetailOut, status_code=201)
def create_thread(
    patient_id: int,
    req: CreateThreadRequest,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """A patient starts a new thread with their care team.

    Round-1 review (2026-08-23): permission + grant alone let a GRANTED
    CLINICIAN call this route directly and originate a thread — contrary to
    the client's UX (a clinician replies to a thread, closes it, reopens
    it, but does not compose the first message) and to this route's own
    docstring, which claimed a patient-only action the code did not
    actually enforce. Fixed by requiring BOTH: the actor's own role is
    'patient' AND their own users.patient_id equals the patient_id in the
    path — not merely that they hold a grant for it. A patient's account
    only ever has one self-grant, so in the correct case this is redundant
    with the grant check; it is required so it cannot be true for anyone
    else, permission and grant included.
    """
    _verify_internal_token(x_internal_token)
    _authorize_or_deny(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name, x_request_id=x_request_id,
        patient_id=patient_id, required_permission="messages.write",
        audit_action="messages_thread_create",
    )
    actor_id = parse_user_id(x_actor_id)
    if actor_id is None:
        raise HTTPException(status_code=403, detail="not authorized")

    actor = db.get(User, actor_id)
    if actor is None or actor.role != "patient" or actor.patient_id != patient_id:
        raise HTTPException(status_code=403, detail="not authorized")

    subject = _clean_text(req.subject, max_len=_MESSAGE_SUBJECT_MAX, field="subject")
    body = _clean_text(req.body, max_len=_MESSAGE_BODY_MAX, field="body")
    idempotency_key = (req.idempotency_key or "").strip()
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency_key is required")

    try:
        thread = messaging.create_thread(
            db, patient_id=patient_id, sender_user_id=actor_id,
            subject=subject, body=body, idempotency_key=idempotency_key,
        )
        # Committed together with its audit row (round-1 review): a separate
        # _write_audit call commits on its own, so an audit-write failure
        # could 503 a request whose thread/message was already durable, with
        # no trace of it — the same failure decide_review's own comment
        # documents and fixes the same way.
        db.add(
            AuditLog(
                actor=_actor_label(x_actor_name, x_actor_id),
                message=f"messages_thread_create thread_id={thread.id} patient_id={patient_id}",
            )
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("messaging: thread create failed patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="could not start a new thread right now")

    patient = db.get(Patient, patient_id)
    return ThreadDetailOut(
        id=thread.id, patient_id=thread.patient_id,
        patient_name=patient.name if patient else None,
        subject=thread.subject, status=thread.status,
        created_at=thread.created_at.isoformat() if thread.created_at else "",
        messages=messaging.messages_for(db, thread.id),
    )


@app.get("/threads/{thread_id}", response_model=ThreadDetailOut)
def get_thread(
    thread_id: int,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """A thread and its full message history. Reading it also advances the
    caller's own read position to the latest message — the same "the read
    IS the state change" shape /patients/{id}/summary already uses for the
    review queue, applied here to unread counts instead."""
    _verify_internal_token(x_internal_token)
    actor_id = _authorize_messaging(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name,
        required_permission="messages.read", audit_action="messages_thread_read",
    )
    try:
        thread = messaging.thread_for_actor(
            db, thread_id=thread_id, authorized_patient_ids=active_patient_ids_query(actor_id)
        )
        if thread is None:
            raise HTTPException(status_code=404, detail="thread not found")
        rows = messaging.messages_for(db, thread.id)
        if rows:
            messaging.mark_read(db, thread_id=thread.id, user_id=actor_id, last_message_id=rows[-1]["id"])
        patient = db.get(Patient, thread.patient_id)
        db.commit()
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("messaging: thread read failed thread_id=%s error_type=%s", thread_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="messages temporarily unavailable")

    return ThreadDetailOut(
        id=thread.id, patient_id=thread.patient_id,
        patient_name=patient.name if patient else None,
        subject=thread.subject, status=thread.status,
        created_at=thread.created_at.isoformat() if thread.created_at else "",
        messages=rows,
    )


@app.post("/threads/{thread_id}/messages", response_model=MessageOut, status_code=201)
def reply_to_thread(
    thread_id: int,
    req: SendMessageRequest,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    _verify_internal_token(x_internal_token)
    actor_id = _authorize_messaging(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name,
        required_permission="messages.write", audit_action="messages_reply",
    )
    body = _clean_text(req.body, max_len=_MESSAGE_BODY_MAX, field="body")
    idempotency_key = (req.idempotency_key or "").strip()
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency_key is required")

    try:
        thread = messaging.thread_for_actor(
            db, thread_id=thread_id, authorized_patient_ids=active_patient_ids_query(actor_id)
        )
        if thread is None:
            raise HTTPException(status_code=404, detail="thread not found")
        message = messaging.send_message(
            db, thread=thread, sender_user_id=actor_id, body=body, idempotency_key=idempotency_key,
        )
        # Committed together with its audit row — see create_thread's own
        # comment on why a separate _write_audit call is wrong here.
        db.add(
            AuditLog(
                actor=_actor_label(x_actor_name, x_actor_id),
                message=f"messages_reply thread_id={thread_id} message_id={message.id}",
            )
        )
        db.commit()
    except messaging.MessagingError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("messaging: reply failed thread_id=%s error_type=%s", thread_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="could not send that message")

    sender = db.get(User, actor_id)
    return MessageOut(
        id=message.id, thread_id=message.thread_id, sender_user_id=actor_id,
        sender_name=(sender.full_name or sender.username) if sender else "Unknown",
        body=message.body, created_at=message.created_at.isoformat() if message.created_at else "",
    )


@app.post("/threads/{thread_id}/status", response_model=ThreadDetailOut)
def set_thread_status(
    thread_id: int,
    req: ThreadStatusRequest,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_actor_name: Optional[str] = Header(default=None, alias="X-Actor-Name"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """Close or reopen. Staff-only — per the client's UX, a patient reads and
    replies but does not control the thread's lifecycle."""
    _verify_internal_token(x_internal_token)
    actor_id = _authorize_messaging(
        db, x_actor_id=x_actor_id, x_actor_name=x_actor_name,
        required_permission="messages.write", audit_action="messages_set_status",
    )
    if _actor_role(db, actor_id) == "patient":
        raise HTTPException(status_code=403, detail="not authorized")

    try:
        thread = messaging.thread_for_actor(
            db, thread_id=thread_id, authorized_patient_ids=active_patient_ids_query(actor_id)
        )
        if thread is None:
            raise HTTPException(status_code=404, detail="thread not found")
        messaging.set_status(db, thread=thread, status=req.status)
        # Committed together with its audit row — see create_thread's own
        # comment on why a separate _write_audit call is wrong here.
        db.add(
            AuditLog(
                actor=_actor_label(x_actor_name, x_actor_id),
                message=f"messages_set_status thread_id={thread_id} status={req.status}",
            )
        )
        db.commit()
    except messaging.MessagingError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        log.error("messaging: status change failed thread_id=%s error_type=%s", thread_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="could not update that thread")

    return ThreadDetailOut(
        id=thread.id, patient_id=thread.patient_id, subject=thread.subject, status=thread.status,
        created_at=thread.created_at.isoformat() if thread.created_at else "",
        messages=messaging.messages_for(db, thread.id),
    )


_POLICY_QUESTION_MAX = 500


@app.post("/policy/ask", response_model=PolicyAnswerOut)
def ask_policy_navigator(
    req: PolicyQuestionRequest,
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    db: Session = Depends(get_db),
):
    """Read-only, stateless: explains approved synthetic policy for the
    caller's own role-derived audience/workflow scope
    (libs/policy_navigator.scope_for_role). No patient_id, no grant check —
    this never touches patient data, only the scope-filtered corpus. Not
    persisted; nothing here writes an audit_logs row or a draft."""
    _verify_internal_token(x_internal_token)
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question must not be blank")
    if len(question) > _POLICY_QUESTION_MAX:
        raise HTTPException(status_code=422, detail=f"question must be at most {_POLICY_QUESTION_MAX} characters")

    actor_role = _actor_role(db, parse_user_id(x_actor_id))
    result = policy_navigator_path.ask_policy_navigator(question, actor_role=actor_role)
    return PolicyAnswerOut(
        answer=result.answer,
        citations=[
            PolicyCitationOut(
                citation_id=c.citation_id, source_id=c.source_id, source_version=c.source_version,
                title=c.title, section_id=c.section_id,
            )
            for c in result.citations
        ],
        label=result.label,
        termination_reason=result.termination_reason,
    )
