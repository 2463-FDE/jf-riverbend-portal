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
      - Current state: _intake_log_summary logs only correlation_id,
        created_via, and consent_count (a bare integer, not which consents)
        — nothing health- or payment-derived, and nothing that combines with
        the adjacent patient_id line to reveal a sensitive attribute about
        that patient.
    This does not retroactively scrub prior log entries written before this
    fix — see docs/runbook.md for that gap.
  * D5 — no master patient index / match key: every /intake creates a brand new
    patients row, so one person forks into several charts (intake.yaml match_key:
    none).
  * Consents are inserted one at a time (a commit per consent).

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
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from libs.tracing import new_correlation_id, safe_span
from logging_config import configure
from models import Consent, InsuranceCoverage, Patient
from schemas import Demographics, Insurance, IntakeRequest, IntakeResponse

log = configure(settings.service_name)
app = FastAPI(title="Riverbend intake-service", version="1.4.0")

_TRACER_NAME = "intake-service"

INTAKE_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "intake.yaml")


@app.get("/healthz")
def healthz():
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
):
    started = time.time()
    correlation_id = x_request_id or new_correlation_id()

    # D1 (Week 1 catch-up fix, revised twice): log an explicit allowlist
    # summary containing no health/payment-derived or patient-linkable
    # metadata — see _intake_log_summary.
    log.info('POST /intake summary=%s', json.dumps(_intake_log_summary(req, correlation_id)))

    with safe_span(_TRACER_NAME, "intake.create", {"correlation_id": correlation_id}) as span:
        # D5 (flagged, not fixed): no MPI / match-key lookup on (name, dob, ssn).
        # Every intake inserts a brand new chart, even for a returning patient.
        patient_id = _create_patient(db, req.demographics)

        coverage_id = None
        if req.insurance is not None:
            coverage_id = _create_coverage(db, patient_id, req.insurance)

        _record_consents(db, patient_id, req.consents)

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
    )


_INTAKE_LOG_SUMMARY_KEYS = frozenset({"correlation_id", "created_via", "consent_count"})


def _intake_log_summary(req: IntakeRequest, correlation_id: str) -> dict[str, Any]:
    """Allowlist-only summary of an /intake request, safe to log at INFO.

    Deliberately an allowlist, not a blocklist: only the fields named here
    (see _INTAKE_LOG_SUMMARY_KEYS) can ever appear in the intake log line,
    regardless of what fields exist on IntakeRequest/Demographics/Insurance
    today or are added to them in the future.

    Two PR review rounds narrowed this down:
      1. A blocklist-based redact() call on the whole body still leaked
         insurance.member_id/group_number (not in the blocklist).
      2. A first allowlist attempt still logged consents, has_dob/has_ssn/...
         presence flags, and insurance_plan_type — none of those are raw
         identifiers, but the very next log line records the newly created
         patient_id, so log-order correlation could tie a specific patient
         to a health/payment attribute (e.g. "Medicaid", a declined
         consent). Anything health- or payment-derived, or that varies with
         a patient's answers to a sensitive question, is excluded now, even
         in coarse/boolean form — only operational metadata remains:
         correlation_id (opaque, not PHI-derived), created_via
         (self_service | front_desk — a channel, not a patient attribute),
         and consent_count (how many consents were included, not which).
    """
    return {
        "correlation_id": correlation_id,
        "created_via": req.demographics.created_via,
        "consent_count": len(req.consents),
    }


def _create_patient(db: Session, demo: Demographics) -> int:
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
        db.commit()
        db.refresh(patient)
        return patient.id
    except SQLAlchemyError as e:
        db.rollback()
        log.error("intake: failed to create patient: %s", e)
        raise HTTPException(status_code=503, detail="patient store unavailable")


def _create_coverage(db: Session, patient_id: int, ins: Insurance) -> int:
    try:
        coverage = InsuranceCoverage(
            patient_id=patient_id,
            payer_name=ins.payer_name,
            member_id=ins.member_id,
            group_number=ins.group_number,
            plan_type=ins.plan_type,
        )
        db.add(coverage)
        db.commit()
        db.refresh(coverage)
        return coverage.id
    except SQLAlchemyError as e:
        db.rollback()
        log.error("intake: failed to record coverage for patient %s: %s", patient_id, e)
        raise HTTPException(status_code=503, detail="coverage store unavailable")


def _record_consents(db: Session, patient_id: int, kinds: list[str]) -> None:
    # Inefficient by design: one INSERT + COMMIT per consent (a separate
    # transaction round-trip each) rather than a single batched insert.
    for kind in kinds:
        try:
            db.add(Consent(patient_id=patient_id, kind=kind))
            db.commit()
        except SQLAlchemyError as e:
            db.rollback()
            log.error("intake: failed to record consent %s for patient %s: %s", kind, patient_id, e)


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
