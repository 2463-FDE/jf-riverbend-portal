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
from models import AuditLog, Encounter, Patient, Record, User
from patient_access_gate import (
    SqlPatientAccessGate,
    active_patient_ids_query,
    parse_user_id,
)
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
        log.exception("startup grant-coverage check failed")
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
    _authorize_actor_permission(
        db,
        x_actor_id=x_actor_id,
        x_actor_name=x_actor_name,
        required_permission="patients.read",
        audit_action="list_patients",
    )
    actor = x_actor_name or x_actor_id or "unknown"
    user_id = parse_user_id(x_actor_id)
    if user_id is None:
        _write_audit(db, actor=actor, message="list_patients denied: no valid actor")
        return PatientPage(items=[], total=0, limit=limit, offset=offset)

    try:
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
    except SQLAlchemyError:
        log.exception("list_patients: database error")
        raise HTTPException(status_code=503, detail="database unavailable")

    _write_audit(
        db,
        actor=actor,
        message=f"list_patients returned {len(rows)} patient(s): {sorted(p.id for p in rows)}",
    )
    return PatientPage(
        items=[PatientSummary.model_validate(p) for p in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _actor_label(x_actor_name: Optional[str], x_actor_id: Optional[str]) -> str:
    """Human-legible audit actor: username (X-Actor-Name) when the gateway
    forwarded it, else the stable users.id (X-Actor-Id), else 'unknown'.
    Authorization always uses the id; this is display/audit only."""
    return x_actor_name or x_actor_id or "unknown"


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
    except SQLAlchemyError:
        # A policy store we cannot read is not a permitted request. Fail
        # closed and say so, rather than degrading into an allow.
        log.exception("permission check: database error for actor")
        raise HTTPException(status_code=503, detail="authorization store unavailable")

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
    _authorize_actor_permission(
        db,
        x_actor_id=x_actor_id,
        x_actor_name=x_actor_name,
        required_permission=required_permission,
        audit_action=audit_action,
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
    except SQLAlchemyError:
        log.exception("get_patient: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")

    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    _write_audit(
        db,
        actor=_actor_label(x_actor_name, x_actor_id),
        message=f"get_patient outcome=allowed patient_id={patient_id} correlation_id={x_request_id or ''}",
    )
    return PatientDetail.model_validate(patient)


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
    except SQLAlchemyError:
        log.exception(
            "get_patient_records: database error for patient_id=%s", patient_id
        )
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
    except SQLAlchemyError:
        db.rollback()
        log.exception("patient_view: failed to write audit_logs entry")
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

    _authorize_actor_permission(
        db,
        x_actor_id=x_actor_id,
        x_actor_name=x_actor_name,
        required_permission=required_permission,
        audit_action=audit_action,
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
    except SQLAlchemyError:
        log.exception("get_patient_view: database error for patient_id=%s", patient_id)
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
    except SQLAlchemyError:
        log.exception("get_patient_reconciliation: database error for patient_id=%s", patient_id)
        raise HTTPException(status_code=503, detail="database unavailable")
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    try:
        result = build_reconciliation_result(
            db, patient_id, patient, x_request_id or "", actor_id=x_actor_id or ""
        )
    except SQLAlchemyError:
        log.exception("get_patient_reconciliation: database error for patient_id=%s", patient_id)
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
    _authorize_actor_permission(
        db,
        x_actor_id=x_actor_id,
        x_actor_name=x_actor_name,
        required_permission="records.read",
        audit_action="records_search",
    )
    actor = x_actor_name or x_actor_id or "unknown"
    user_id = parse_user_id(x_actor_id)
    if user_id is None:
        _write_audit(db, actor=actor, message="records_search denied: no valid actor")
        return []

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
    except SQLAlchemyError:
        log.exception("search_records: database error")
        raise HTTPException(status_code=503, detail="database unavailable")

    _write_audit(
        db,
        actor=actor,
        message=f"records_search returned {len(rows)} hit(s) across patients {sorted({r.patient_id for r in rows})}",
    )
    return [RecordSearchHit.model_validate(r) for r in rows]
