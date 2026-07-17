"""
intake-service — multi-step patient registration + insurance + consent capture.

Both the front desk and the self-service portal POST a full intake payload here.
We create the patient chart, attach insurance coverage (if supplied), record the
signed consents, and verify payer eligibility before returning.

Inherited shortcomings (left as-is from the handoff):
  * D1 — the full request body (PHI: name/dob/ssn/notes) is written to a file
    log at INFO. See logging_config.py.
  * D5 — no master patient index / match key: every /intake creates a brand new
    patients row, so one person forks into several charts (intake.yaml match_key:
    none).
  * D4 / RIV-088 — eligibility is verified inline on the request thread with no
    timeout, so a slow payer makes registration "spin ~4-5s".
  * Consents are inserted one at a time (a commit per consent).
"""
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from logging_config import configure
from models import Consent, InsuranceCoverage, Patient
from schemas import Demographics, Insurance, IntakeRequest, IntakeResponse

log = configure(settings.service_name)
app = FastAPI(title="Riverbend intake-service", version="1.3.0")

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
def create_intake(req: IntakeRequest, db: Session = Depends(get_db)):
    started = time.time()

    # D1 (flagged, not fixed): persist the entire request body — including PHI —
    # to the file handler so the front desk has a record of every registration.
    log.info('POST /intake body=%s', req.model_dump_json())

    # D5 (flagged, not fixed): no MPI / match-key lookup on (name, dob, ssn).
    # Every intake inserts a brand new chart, even for a returning patient.
    patient_id = _create_patient(db, req.demographics)

    if req.insurance is not None:
        _create_coverage(db, patient_id, req.insurance)

    # D4 / RIV-088 (flagged, not fixed): eligibility is verified INLINE on this
    # request thread, synchronously and with NO timeout — so a slow payer blocks
    # the whole /intake call. The cohort's fix is to make this async / bounded.
    eligibility = _verify_eligibility(req.insurance)

    _record_consents(db, patient_id, req.consents)

    elapsed = round(time.time() - started, 2)
    log.info("POST /intake 201 patient_id=%s elapsed=%.2fs", patient_id, elapsed)
    return IntakeResponse(patient_id=patient_id, elapsed_seconds=elapsed, eligibility=eligibility)


def _create_patient(db: Session, demo: Demographics) -> int:
    try:
        patient = Patient(
            name=demo.name,
            dob=demo.dob,
            ssn=demo.ssn,
            gender=demo.gender,
            address=demo.address,
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


def _create_coverage(db: Session, patient_id: int, ins: Insurance) -> None:
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


def _verify_eligibility(ins: Optional[Insurance]) -> Optional[dict[str, Any]]:
    if ins is None or not ins.member_id:
        return None

    # RIV-088 / D4: the artificial sleep stands in for the blocking clearinghouse
    # round-trip the front desk experiences; the httpx call below has NO timeout,
    # so a hung payer hangs /intake. This BLOCKS the request thread by design.
    time.sleep(4.2)
    try:
        resp = httpx.get(
            f"{settings.eligibility_url}/eligibility",
            params={"insurance_id": ins.member_id},
        )  # no timeout= — synchronous, blocks /intake (RIV-088)
        return resp.json()
    except Exception as e:
        # Stage 1 fix: failing to REACH eligibility-service is a transport
        # failure, not evidence the member's coverage is inactive — map it to
        # "unknown" (see services/eligibility-service/contracts.py::EligibilityStatus),
        # never "active": False on its own. Log/return the exception TYPE
        # only, never str(e) — it can echo the request URL, which includes
        # the member_id.
        log.error("intake: eligibility check unreachable (error_type=%s)", type(e).__name__)
        return {
            "insurance_id": ins.member_id,
            "active": False,
            "status": "unknown",
            "payer": ins.payer_name,
            "raw_status": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "error": type(e).__name__,
        }
