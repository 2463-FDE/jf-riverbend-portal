"""
intake-service — multi-step patient registration + insurance + consent capture.

Both the front desk and the self-service portal POST a full intake payload here.
We create the patient chart, attach insurance coverage (if supplied), record the
signed consents, and enqueue an async payer eligibility check before returning.

Inherited shortcomings (left as-is from the handoff):
  * D1 — RESOLVED (Week 1 catch-up, revised twice after PR review): the
    request body previously written to the file log at INFO carried PHI
    (name/dob/ssn/address/notes) in plain text.
      - Revision 1 ran the body through libs/safe_logging.redact() first,
        but that is a *blocklist* — a review caught that insurance
        identifiers (member_id/group_number) still leaked because they
        weren't in the blocklist.
      - Revision 2 switched to an *allowlist* (_intake_log_summary) instead
        of any form of the request body — but a second review caught that
        the allowlist itself still logged patient-linked, health/payment-
        derived metadata (consents, has_dob/has_ssn/... presence flags,
        insurance_plan_type). The very next log line records the newly
        created patient_id, so ordinary chronological log correlation could
        tie e.g. "Medicaid" or a declined consent back to a specific patient
        — a real re-identification/inference risk even with no raw
        identifier present.
      - Revision 3 fixed created_via (validated to a closed set) and a
        migration hardening issue, but a fourth review caught two more:
        (a) `correlation_id` was taken verbatim from the caller-supplied
        `X-Request-Id` header with no validation — intake-service is
        exposed directly on the host (docker-compose.yml, port 8071), so
        any caller could put PHI-shaped text in that header and have it
        logged, bypassing the allowlist entirely; (b) `consent_count`
        still revealed a patient-linkable signal, since 2 required consents
        is the baseline and a count of 3-4 discloses that the patient
        accepted an optional (financial/communications) consent.
      - Current state: correlation_id is only ever trusted from the caller
        if it's UUID-shaped (see _safe_correlation_id); anything else is
        replaced with a fresh server-generated one before it's used
        anywhere, logging included. consent_count is removed from the log
        summary entirely — _intake_log_summary now logs only
        correlation_id and created_via, nothing that varies with a
        patient's answers to any question on the form.
    This does not retroactively scrub prior log entries written before this
    fix — see docs/runbook.md for that gap.
  * D5 — PARTIALLY RESOLVED (Week 2-3 catch-up, adr/0004/RIV-160): every
    /intake used to create a brand-new patients row unconditionally, no
    matter how many existing rows already matched the same person (the
    seeded Maria Gonzalez fixture — 3 rows, one penicillin allergy visible
    from only one of them). intake.yaml's match_key hook is still `none`
    (this fix lives in code, not that config), but /intake now runs a
    deterministic (dob, ssn) match-key lookup — see _find_match_candidates —
    before creating a patient:
      - An EXACT match (same ssn, same dob) blocks silent creation with a
        409 unless the caller explicitly resolves it via
        `duplicate_override="create_new"`, which proceeds anyway but still
        records the resemblance in patient_links so it's reviewable, not
        silent.
      - A PARTIAL match (same ssn, different dob) never blocks — it
        proceeds exactly as before, but records an unconfirmed
        patient_links row and returns `possible_duplicate_match=True` for
        staff to review later.
      - No match, or no ssn supplied at all, behaves exactly as before this
        fix — this only engages when there's a reliable key to compare.
      - Concurrent /intake calls for the same ssn are serialized with a
        transaction-scoped Postgres advisory lock (PR #20 round-8 review:
        the match-then-create sequence is otherwise a check-then-insert race
        — two simultaneous requests for a brand-new ssn could both see no
        candidates and both create a row, with neither recording a
        patient_links entry) — see _match_key_lock.
    This does NOT retroactively merge/backfill any duplicate that already
    existed before this migration (adr/0004 explicitly scopes that out —
    Maria Gonzalez's 3 existing rows stay 3 rows) and does not implement the
    staff-confirmation UI (API/backend only this stage).
      - PR #20 round-8 review also caught that the original version of this
        fix returned exact/partial candidate patient_ids straight to the
        caller and let an unauthenticated caller attach coverage/consents to
        an existing patient via `duplicate_override="link_to_existing"` +
        caller-supplied `link_to_patient_id`. intake-service has no auth
        dependency and is exposed directly on the host (docker-compose.yml,
        port 8071), so that was both a patient-enumeration oracle and an
        unauthorized chart-modification path. Fixed by dropping
        `link_to_existing` entirely and never returning a candidate
        patient_id from this endpoint — see IntakeResponse.possible_duplicate_match
        and the 409 detail in create_intake. Staff-mediated linking onto an
        existing patient is deferred until there's a trusted,
        staff-authenticated path with a server-derived actor identity.
      - PR #20 round-11 review: the gateway already requires a valid staff
        session before it will forward to /intake (services/gateway/app.py::
        proxy_intake), but intake-service had no way to tell a genuine
        gateway-forwarded call apart from a caller hitting its own published
        host port directly (docker-compose.yml, port 8071) — which bypassed
        that session check entirely and let an unauthenticated caller probe
        the exact/partial/no-match distinction above as a patient/SSN-
        existence oracle. _verify_internal_token (same shared-secret pattern
        as records-service's StaffAccessGate path) now rejects any call that
        doesn't carry the gateway's INTERNAL_SERVICE_TOKEN, before any
        duplicate-detection logic runs. This is a transport-trust check
        (proves the call came through the gateway), not per-patient
        authorization, and it does not change the authenticated, staff-
        mediated intake flow itself. Whether Riverbend should ever offer
        true public/unauthenticated self-service registration (this
        module's opening paragraph's "self-service portal" line) is a
        separate, undecided product question — not resolved by this fix.
  * Consents are inserted one at a time (a flush per consent, no commit
    until the end — see round-13 below). PR #20 round-12 review: a failed
    consent insert used to be swallowed (rollback + log, no raise), so
    /intake could return 201 with a real patient_id even though a required
    consent (npp_ack, treatment_consent) never persisted — an irreversible
    partial registration reported as success. _record_consents now raises
    HTTPException(503) on any consent write failure, matching
    _create_patient/_create_coverage's existing convention — see
    _record_consents.
  * Round-13 review (2026-08-06): round-12's fix made the 503 honest, but
    the patient and coverage rows were each already committed
    independently (_create_patient/_create_coverage each did their own
    db.commit()) before _record_consents ever ran, and every consent
    committed separately too. A consent failure therefore still stranded a
    real, committed patient (and coverage) row with no consent attached —
    and a retry with the same ssn+dob now trips the round-8/round-10
    exact-match 409, so staff could not complete the registration without
    someone editing the database by hand. _create_patient,
    _create_patient_with_links, _create_coverage, and _record_consents now
    only flush (never commit) — create_intake commits the whole
    patient+coverage+consents group exactly once, after every step
    succeeds. Any failure at any step rolls back everything flushed so far
    in the same transaction, so a failed intake never leaves a partial
    patient behind for a retry to collide with.
  * Round-13 review, second finding (2026-08-06): INTERNAL_SERVICE_TOKEN
    (see _verify_internal_token) defaults to an empty string and was only
    ever checked per-request — a container starts and passes
    docker-compose's healthcheck (which just hits /healthz) even with the
    token unset, so a misconfigured deploy looks healthy while every
    gateway-forwarded /intake call 401s. /healthz now fails the same
    presence/length check _verify_internal_token uses on every real
    request, so a missing/placeholder token surfaces as a failing
    healthcheck (and `docker compose ps` showing "unhealthy") instead of a
    silent, healthy-looking outage. Same fix applied to gateway's and
    records-service's /healthz for the same reason — see each service's
    app.py.

Stage 3 (RIV-088 / RIV-141 fix): eligibility used to be verified INLINE on
this request thread with no timeout — a slow or down payer blocked /intake
for seconds to (per the runbook) tens of minutes. Patient, coverage, and
consent rows now persist first, unconditionally; the payer check itself is
handed off to eligibility-service's Redis-backed job queue (jobs.py/worker.py
there) via one bounded, fast enqueue call — see _start_eligibility_check.
/intake returns 201 promptly either way, with `eligibility_status` and a
safe opaque `eligibility_job_id` for the caller to poll. The old
`IntakeResponse.eligibility` dict field is kept, populated with a
pending/degraded summary, for backward compatibility with any existing
caller that reads it.
"""
import hmac
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from libs.tracing import new_correlation_id, safe_span
from logging_config import configure
from models import Consent, InsuranceCoverage, Patient, PatientAccessGrant, PatientLink
from schemas import Demographics, Insurance, IntakeRequest, IntakeResponse

log = configure(settings.service_name)

_MIN_INTERNAL_TOKEN_LENGTH = 32  # rejects "changeme" and any other short/example value


def _internal_token_is_configured() -> bool:
    """Round-13 review (2026-08-06): the same presence/length floor
    _verify_internal_token enforces on every request, checked once here so
    /healthz can fail before a misconfigured deploy ever serves a real
    request. See the module docstring's round-13 entry."""
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
    uvicorn-started process. See gateway's and records-service's identical
    fix."""
    if not _internal_token_is_configured():
        raise RuntimeError(
            f"INTERNAL_SERVICE_TOKEN is not set (or is shorter than "
            f"{_MIN_INTERNAL_TOKEN_LENGTH} chars) — refusing to start. Set a real "
            f"random value (e.g. `openssl rand -hex 32`) in .env; see .env.example."
        )
    yield


app = FastAPI(title="Riverbend intake-service", version="1.4.0", lifespan=lifespan)

_TRACER_NAME = "intake-service"

INTAKE_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "intake.yaml")

# UUID-shaped, with or without hyphens — matches libs.tracing.new_correlation_id()
# (uuid4().hex) and standard hyphenated UUIDs. Fixed length, hex charset only:
# cannot carry free-text/PHI, regardless of length or content of what a caller
# sends in X-Request-Id.
_CORRELATION_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
)


_MIN_INTERNAL_TOKEN_LENGTH = 32  # rejects "changeme" and any other short/example value


def _verify_internal_token(x_internal_token: Optional[str]) -> None:
    """Round-11 review fix (2026-08-05): proves this call came through the
    gateway (which already requires a staff session — services/gateway/
    app.py::proxy_intake), not a direct caller hitting this service's
    published host port (docker-compose.yml, port 8071). Mirrors
    services/records-service/app.py::_verify_internal_token exactly — same
    shared INTERNAL_SERVICE_TOKEN, same fail-closed semantics: an unset/empty
    configured token, or one shorter than _MIN_INTERNAL_TOKEN_LENGTH (a
    human-typed placeholder like "changeme"), must never be treated as "no
    check needed" or accepted as a real secret.
    """
    configured = settings.internal_service_token
    if (
        not configured
        or len(configured) < _MIN_INTERNAL_TOKEN_LENGTH
        or not x_internal_token
        or not hmac.compare_digest(x_internal_token, configured)
    ):
        raise HTTPException(status_code=401, detail="missing or invalid internal service token")


def _safe_correlation_id(x_request_id: Optional[str]) -> str:
    """Only trust a caller-supplied X-Request-Id if it's UUID-shaped;
    otherwise generate a fresh server-side one (PR #20 review: intake-service
    is exposed directly on the host — docker-compose.yml, port 8071 — so this
    header is untrusted input like any other, and a caller could otherwise
    put PHI-shaped text in it and have it persisted verbatim wherever
    correlation_id is logged or forwarded)."""
    if x_request_id and _CORRELATION_ID_PATTERN.fullmatch(x_request_id):
        return x_request_id
    return new_correlation_id()


@app.get("/healthz")
def healthz():
    if not _internal_token_is_configured():
        raise HTTPException(status_code=503, detail="internal_service_token not configured")
    return {"status": "ok", "service": settings.service_name}


@app.get("/intake/config")
def intake_config():
    """Return the parsed intake.yaml so the front-desk UI can adapt its form."""
    try:
        with open(INTAKE_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.error("intake config missing at %s", INTAKE_CONFIG_PATH)
        raise HTTPException(status_code=500, detail="intake config not found")
    except yaml.YAMLError as e:
        log.error("intake config parse error: %s", e)
        raise HTTPException(status_code=500, detail="intake config invalid")


@app.post("/intake", response_model=IntakeResponse, status_code=201)
def create_intake(
    req: IntakeRequest,
    db: Session = Depends(get_db),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
    x_actor_id: Optional[str] = Header(default=None, alias="X-Actor-Id"),
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
):
    _verify_internal_token(x_internal_token)

    started = time.time()
    correlation_id = _safe_correlation_id(x_request_id)

    # D1 (Week 1 catch-up fix, revised twice): log an explicit allowlist
    # summary containing no health/payment-derived or patient-linkable
    # metadata — see _intake_log_summary.
    log.info('POST /intake summary=%s', json.dumps(_intake_log_summary(req, correlation_id)))

    with safe_span(_TRACER_NAME, "intake.create", {"correlation_id": correlation_id}) as span:
        # PR #20 round-8 review: check-then-insert on (ssn, dob) is a race
        # between two concurrent /intake calls for the same new patient —
        # serialize per-ssn with a transaction-scoped advisory lock, held
        # until the first commit below (patient create or link record) ends
        # this transaction and releases it.
        #
        # D5 (Week 2-3 catch-up, adr/0004/RIV-160): deterministic (dob, ssn)
        # match-key lookup before creating a patient — see module docstring.
        #
        # Round-20 review (2026-08-06): both calls issue real statements (an
        # advisory-lock acquisition, a SELECT) but previously sat outside
        # any SQLAlchemyError handler — a DB timeout or statement failure
        # here surfaced as an unhandled 500 instead of this service's
        # existing rollback-then-503 convention (_create_patient/
        # _create_coverage/_record_consents). Wrapped the same way now.
        try:
            _acquire_match_key_lock(db, req.demographics)
            exact_ids, partial_ids = _find_match_candidates(db, req.demographics)
        except SQLAlchemyError as e:
            db.rollback()
            log.error("intake: failed to check for duplicate patients (error_type=%s)", type(e).__name__)
            raise HTTPException(status_code=503, detail="patient store unavailable")
        span.set_attribute("exact_match_count", len(exact_ids))
        span.set_attribute("partial_match_count", len(partial_ids))

        possible_duplicate_match = False
        if exact_ids:
            # Round-10 review (2026-08-05): this used to be overridable via a
            # caller-supplied duplicate_override="create_new" + confirmed_by —
            # removed entirely (see schemas.IntakeRequest). intake-service has
            # no auth dependency, so nothing stopped any caller from both
            # forcing a duplicate through AND forging who "confirmed" it. An
            # exact SSN+DOB match now always blocks here, no exceptions, from
            # this endpoint.
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "possible_duplicate_patient",
                    "confidence": "exact",
                },
            )
        elif partial_ids:
            # Partial matches never block — recorded unconfirmed for staff
            # review, surfaced back only as a boolean (never the candidate
            # patient_id — see IntakeResponse.possible_duplicate_match).
            # Round-3 review (2026-08-05): patient + link rows are one
            # atomic write — see _create_patient_with_links.
            patient_id = _create_patient_with_links(db, req.demographics, partial_ids)
            possible_duplicate_match = True
        else:
            patient_id = _create_patient(db, req.demographics)

        coverage_id = None
        if req.insurance is not None:
            coverage_id = _create_coverage(db, patient_id, req.insurance)

        _record_consents(db, patient_id, req.consents)

        # PR #23 (Week 4 catch-up): grant the registering staff member access to
        # the chart they just created, in the SAME atomic group as the patient
        # write — so front-desk registration doesn't depend on the now
        # patient-scoped /patients roster (records-service list_patients). Only
        # when an authenticated actor is present (front-desk flow); self-service
        # intake carries no X-Actor-Id and creates no grant, so those patients
        # need an explicit grant before any staff can view them (docs/runbook.md).
        _grant_registrar_access(db, x_actor_id, patient_id)

        # Round-13 review (2026-08-06): patient, coverage, and every consent
        # are only flushed above, not committed — this is the single commit
        # that makes the whole group durable together. If any step above
        # failed, its own except block already rolled back and raised a 503
        # before this line, so this commit only ever runs once every write
        # in the group has succeeded in the same transaction. This also
        # releases the _acquire_match_key_lock advisory lock taken above.
        try:
            db.commit()
        except SQLAlchemyError as e:
            db.rollback()
            log.error("intake: failed to finalize patient/coverage/consent commit (error_type=%s)", type(e).__name__)
            raise HTTPException(status_code=503, detail="patient store unavailable")

        # Patient/coverage/consent are already committed above, independently
        # of whatever happens next — the fix for RIV-088/RIV-141 is that this
        # step can never block or fail the registration itself.
        eligibility, eligibility_status, eligibility_job_id = _start_eligibility_check(
            req.insurance, patient_id, coverage_id, correlation_id
        )
        span.set_attribute("eligibility_status", eligibility_status or "not_applicable")
        if eligibility_job_id:
            span.set_attribute("eligibility_job_id", eligibility_job_id)

    elapsed = round(time.time() - started, 2)
    log.info(
        "POST /intake 201 patient_id=%s elapsed=%.2fs eligibility_status=%s",
        patient_id,
        elapsed,
        eligibility_status,
    )
    return IntakeResponse(
        patient_id=patient_id,
        elapsed_seconds=elapsed,
        eligibility=eligibility,
        eligibility_status=eligibility_status,
        eligibility_job_id=eligibility_job_id,
        possible_duplicate_match=possible_duplicate_match,
    )


_INTAKE_LOG_SUMMARY_KEYS = frozenset({"correlation_id", "created_via"})


def _intake_log_summary(req: IntakeRequest, correlation_id: str) -> dict[str, Any]:
    """Allowlist-only summary of an /intake request, safe to log at INFO.

    Deliberately an allowlist, not a blocklist: only the fields named here
    (see _INTAKE_LOG_SUMMARY_KEYS) can ever appear in the intake log line,
    regardless of what fields exist on IntakeRequest/Demographics/Insurance
    today or are added to them in the future.

    Four PR review rounds narrowed this down to just two fields:
      1. A blocklist-based redact() call on the whole body still leaked
         insurance.member_id/group_number (not in the blocklist).
      2. A first allowlist attempt still logged consents, has_dob/has_ssn/...
         presence flags, and insurance_plan_type — none of those are raw
         identifiers, but the very next log line records the newly created
         patient_id, so log-order correlation could tie a specific patient
         to a health/payment attribute (e.g. "Medicaid", a declined
         consent).
      3. created_via was trusted without validating it against its
         documented closed set (fixed in schemas.py).
      4. correlation_id was trusted verbatim from the caller-supplied
         X-Request-Id header (fixed by _safe_correlation_id), and
         consent_count — a bare integer, no consent names — still disclosed
         whether a patient accepted an optional consent (2 required is the
         baseline; 3-4 means they accepted financial and/or communications
         consent), correlatable to the adjacent patient_id line.
    What remains logs no information that varies with anything the patient
    answered on the form: correlation_id (opaque, UUID-validated, not
    PHI-derived) and created_via (self_service | front_desk | unknown — a
    channel, validated against a closed set, not a patient attribute).
    """
    return {
        "correlation_id": correlation_id,
        "created_via": req.demographics.created_via,
    }


def _normalize_ssn(ssn: Optional[str]) -> Optional[str]:
    """Digits only, so "412-55-9981" and "412559981" compare equal. Returns
    None for a blank/missing SSN — never an empty string, so callers can
    treat falsy as "no reliable key"."""
    if not ssn:
        return None
    digits = re.sub(r"\D", "", ssn)
    return digits or None


def _acquire_match_key_lock(db: Session, demo: Demographics) -> None:
    """Serialize concurrent /intake calls for the same normalized ssn (PR #20
    round-8 review): without this, two simultaneous requests for a brand-new
    ssn can both run _find_match_candidates before either commits a
    _create_patient, both see zero candidates, and both create a patient row
    — the exact silent-duplicate failure mode this feature exists to catch,
    now happening unrecorded (no patient_links row either, since each
    request believed it was the first).

    pg_advisory_xact_lock is held for the rest of the current transaction —
    released automatically at the next commit/rollback on this connection.
    Round-13 review (2026-08-06): create_intake now commits the whole
    patient+coverage+consents group in one place, so this lock is held for
    that entire group rather than just the patient/link write — a strictly
    longer, still-correct window (it only needs to outlast the
    match-then-create check, and it does). A no-op when there's no ssn to
    key on, same as _find_match_candidates.
    """
    normalized_ssn = _normalize_ssn(demo.ssn)
    if not normalized_ssn:
        return
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"), {"key": normalized_ssn})


def _find_match_candidates(db: Session, demo: Demographics) -> tuple[list[int], list[int]]:
    """adr/0004 (AUD-09/RIV-160) deterministic match-key lookup: (patient
    ids with an EXACT match, patient ids with only a PARTIAL match).

    Exact = same normalized ssn AND same dob (adr/0004: "dob + full ssn
    agree" -> certain duplicate). Partial = same normalized ssn but a
    different dob (adr/0004's own worked example: the seeded Maria Gonzalez
    rows all share one ssn; 1042/1330 share a dob too (exact), 1588's dob is
    transposed (partial) despite an identical name-adjacent spelling
    difference — name is informative context in the ADR, not what gates
    this tier).

    Returns ([], []) whenever no ssn was supplied — there is no reliable key
    to compare, and matching on name/dob alone isn't part of this proposal
    (name variation is exactly what makes this fixture hard: "Maria
    Gonzalez" / "Maria Gonzales" / "M. Gonzalez").

    Scans every patient row with a non-null ssn and compares in Python
    rather than adding a normalized/indexed ssn column — acceptable at this
    system's current seed-data scale (~hundreds of rows), the same
    "deliberate simplicity" character as records-service's existing
    full-scan search debt (D8). This does NOT scale to a real production
    patient volume without a normalized, indexed column — flagged here
    rather than silently assumed away.
    """
    normalized_ssn = _normalize_ssn(demo.ssn)
    if not normalized_ssn:
        return [], []

    exact_ids: list[int] = []
    partial_ids: list[int] = []
    rows = db.execute(select(Patient).where(Patient.ssn.isnot(None))).scalars().all()
    for row in rows:
        if _normalize_ssn(row.ssn) != normalized_ssn:
            continue
        if demo.dob and row.dob and demo.dob == row.dob:
            exact_ids.append(row.id)
        else:
            partial_ids.append(row.id)
    return exact_ids, partial_ids


def _build_partial_patient_link(patient_id: int, linked_patient_id: int) -> "PatientLink":
    """Builds (does not persist) a non-destructive partial-match audit row
    (adr/0004 item 3) — never merges or rewrites any other table's
    patient_id. basis is a coded reason only, never a raw PHI value (see
    PatientLink docstring in models.py).

    Round-10 review (2026-08-05): this is now the ONLY kind of patient_links
    row this service ever writes — always confirmed=False, confirmed_by=None.
    Exact-match links used to be created too, confirmed=True, via a
    caller-supplied duplicate_override + confirmed_by — removed entirely
    (see schemas.IntakeRequest) because intake-service has no auth
    dependency, so nothing stopped a caller both bypassing the exact-match
    block AND forging who supposedly confirmed it. There is no server-
    derived actor identity anywhere in this system to honestly attribute a
    real confirmation to, so this service no longer claims one exists.
    """
    return PatientLink(
        patient_id=patient_id,
        linked_patient_id=linked_patient_id,
        confidence="partial",
        basis="ssn_match_dob_differs",
        confirmed=False,
        confirmed_by=None,
        confirmed_at=None,
    )


def _create_patient_with_links(db: Session, demo: Demographics, partial_ids: list[int]) -> int:
    """Round-3 review fix (2026-08-05): the new patient row and its adr/0004/
    RIV-160 match-key link audit rows must commit or fail TOGETHER. A prior
    version committed the patient first, then wrote each link row in its own
    separately-swallowed transaction (_record_patient_link, now removed) — so
    a link-write failure (migration 012 not yet applied, a transient
    Postgres error) left a newly created duplicate patient with NO audit
    trail: the exact silent-duplicate-fragment failure mode RIV-160 exists
    to prevent, reachable via this degraded path instead of the happy one.

    Uses flush() (not commit()) to obtain the new patient's id while still
    inside the same transaction as its link rows. Round-13 review
    (2026-08-06): this used to commit here too, independently of the
    coverage/consent writes that follow in create_intake — a failure in one
    of those later steps then left this patient (and its link rows) durably
    committed with no way to undo it. Now this only flushes; create_intake
    commits the whole patient+coverage+consents group exactly once, so ANY
    failure anywhere in that group rolls all of it back together and fails
    the request (503) with nothing left half-written.

    Only ever called for partial matches now (round-10 review) — an exact
    match always blocks with 409 before this function is reached, so no
    caller here can create a "confirmed" link at all.
    """
    try:
        patient = Patient(
            name=demo.name,
            first_name=demo.first_name,
            last_name=demo.last_name,
            dob=demo.dob,
            ssn=demo.ssn,
            gender=demo.gender,
            address=demo.address,
            city=demo.city,
            state=demo.state,
            zip_code=demo.zip_code,
            phone=demo.phone,
            email=demo.email,
            notes=demo.notes,
            created_via=demo.created_via,
        )
        db.add(patient)
        db.flush()  # assigns patient.id without ending the transaction
        for candidate_id in partial_ids:
            db.add(_build_partial_patient_link(patient.id, candidate_id))
        db.flush()
        return patient.id
    except SQLAlchemyError as e:
        db.rollback()
        # Same reasoning as _create_patient: never log str(e) — it can embed
        # the failed statement's bound parameters (name/dob/ssn/address/...).
        log.error("intake: failed to create patient with link audit rows (error_type=%s)", type(e).__name__)
        raise HTTPException(status_code=503, detail="patient store unavailable")


def _grant_registrar_access(db: Session, x_actor_id: Optional[str], patient_id: int) -> None:
    """Insert a patient_access_grant for the registering staff member (users.id
    forwarded as X-Actor-Id) so they can immediately open the chart they just
    created. Flush-only; create_intake's single commit makes it durable with
    the patient row. No actor (self-service intake) → no grant. A write failure
    fails the whole registration closed (503), matching this file's convention."""
    try:
        user_id = int(x_actor_id) if x_actor_id not in (None, "") else None
    except (ValueError, TypeError):
        user_id = None
    if user_id is None:
        return
    try:
        db.add(PatientAccessGrant(user_id=user_id, patient_id=patient_id))
        db.flush()
    except SQLAlchemyError as e:
        db.rollback()
        log.error("intake: failed to grant registrar access (error_type=%s)", type(e).__name__)
        raise HTTPException(status_code=503, detail="patient store unavailable")


def _create_patient(db: Session, demo: Demographics) -> int:
    # Round-13 review (2026-08-06): flushes only — see create_intake's single
    # commit and the module docstring's round-13 entry for why this no
    # longer commits on its own.
    try:
        patient = Patient(
            name=demo.name,
            first_name=demo.first_name,
            last_name=demo.last_name,
            dob=demo.dob,
            ssn=demo.ssn,
            gender=demo.gender,
            address=demo.address,
            city=demo.city,
            state=demo.state,
            zip_code=demo.zip_code,
            phone=demo.phone,
            email=demo.email,
            notes=demo.notes,
            created_via=demo.created_via,
        )
        db.add(patient)
        db.flush()  # assigns patient.id without ending the transaction
        return patient.id
    except SQLAlchemyError as e:
        db.rollback()
        # PR #20 review (round 6): never log str(e) here — SQLAlchemy embeds
        # the failed statement's bound parameters (name/dob/ssn/address/...)
        # in a DBAPIError's string form, so this would otherwise dump PHI to
        # logs/intake-service.log on every insert failure. Error TYPE only,
        # same pattern already used by _start_eligibility_check.
        log.error("intake: failed to create patient (error_type=%s)", type(e).__name__)
        raise HTTPException(status_code=503, detail="patient store unavailable")


def _create_coverage(db: Session, patient_id: int, ins: Insurance) -> int:
    # Round-13 review (2026-08-06): flushes only, same reasoning as
    # _create_patient — create_intake commits the whole group once.
    try:
        coverage = InsuranceCoverage(
            patient_id=patient_id,
            payer_name=ins.payer_name,
            member_id=ins.member_id,
            group_number=ins.group_number,
            plan_type=ins.plan_type,
        )
        db.add(coverage)
        db.flush()  # assigns coverage.id without ending the transaction
        return coverage.id
    except SQLAlchemyError as e:
        db.rollback()
        # Same reasoning as _create_patient: str(e) would embed member_id/
        # group_number in plaintext. patient_id is an internal integer, not
        # PHI, and safe to keep.
        log.error("intake: failed to record coverage for patient %s (error_type=%s)", patient_id, type(e).__name__)
        raise HTTPException(status_code=503, detail="coverage store unavailable")


def _record_consents(db: Session, patient_id: int, kinds: list[str]) -> None:
    # Inefficient by design: one INSERT + FLUSH per consent (a separate
    # statement each) rather than a single batched insert.
    #
    # Round-12 review (2026-08-05): a consent write failure used to be
    # swallowed here (rollback + log, no raise) — create_intake still
    # returned 201 with a real patient_id even though a required consent
    # (e.g. npp_ack, treatment_consent) never persisted, an irreversible
    # partial registration reported as a success with no signal to retry.
    # Now raises HTTPException(503), matching _create_patient/_create_coverage's
    # existing "store unavailable" convention, so the caller sees a failure
    # instead of a false success.
    #
    # Round-13 review (2026-08-06): each consent used to commit
    # independently too, so a failure on e.g. the second consent still left
    # the patient, coverage, and first consent durably committed — a
    # stranded partial registration a retry could not repair (it would trip
    # the exact-match 409 instead). Flushes only now; create_intake commits
    # the whole group once every consent has flushed successfully, so a
    # failure on any consent rolls back the patient/coverage/earlier
    # consents in the same transaction, same as the other write paths above.
    for kind in kinds:
        try:
            db.add(Consent(patient_id=patient_id, kind=kind))
            db.flush()
        except SQLAlchemyError as e:
            db.rollback()
            # kind (e.g. "npp_ack") and patient_id aren't PHI, but str(e) is
            # avoided here too for consistency with the other two handlers.
            log.error(
                "intake: failed to record consent %s for patient %s (error_type=%s)",
                kind, patient_id, type(e).__name__,
            )
            raise HTTPException(status_code=503, detail="consent store unavailable")


def _start_eligibility_check(
    ins: Optional[Insurance],
    patient_id: int,
    coverage_id: Optional[int],
    correlation_id: str,
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[str]]:
    """Enqueue an async eligibility job on eligibility-service instead of the
    old inline, unbounded payer call. Returns (eligibility_dict,
    eligibility_status, eligibility_job_id).

    This one HTTP call is bounded by a short timeout
    (ELIGIBILITY_JOB_ENQUEUE_TIMEOUT_SECONDS) — it only asks eligibility-
    service to enqueue a job (a fast Redis write), never the payer round-trip
    itself, so a slow/down payer can no longer stall /intake. If even this
    bounded call fails, that failure is treated the same way Stage 1 already
    treats an unreachable eligibility-service: "unknown", never "inactive",
    with the exception TYPE only ever logged/returned (never str(e), which
    can echo the request URL/member_id).
    """
    if ins is None or not ins.member_id:
        return None, None, None

    idempotency_key = f"patient:{patient_id}:coverage:{coverage_id}"
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        resp = httpx.post(
            f"{settings.eligibility_url}/eligibility/jobs",
            json={"insurance_id": ins.member_id, "idempotency_key": idempotency_key},
            headers={"X-Request-Id": correlation_id},
            timeout=settings.eligibility_job_enqueue_timeout_seconds,
        )
        resp.raise_for_status()
        job = resp.json()
        eligibility = {
            "insurance_id": ins.member_id,
            "active": False,
            "status": "pending",
            "payer": ins.payer_name,
            "raw_status": None,
            "checked_at": now_iso,
            "stale": False,
            "error": None,
        }
        return eligibility, "pending", job.get("job_id")
    except Exception as e:
        log.error("intake: eligibility job enqueue failed (error_type=%s)", type(e).__name__)
        eligibility = {
            "insurance_id": ins.member_id,
            "active": False,
            "status": "unknown",
            "payer": ins.payer_name,
            "raw_status": None,
            "checked_at": now_iso,
            "stale": False,
            "error": type(e).__name__,
        }
        return eligibility, "unknown", None
