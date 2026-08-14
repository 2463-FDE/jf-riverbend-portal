"""Pydantic v2 response/request schemas for records-service."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PatientSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mrn: str | None = None
    name: str
    dob: str | None = None
    gender: str | None = None
    created_at: datetime | None = None


class PatientDetail(BaseModel):
    """Patient demographics + contact.

    Carries both the composed/legacy display values (`name`, `address`) and
    the structured fields (`first_name`/`last_name`/`city`/`state`/
    `zip_code`) they were composed from — see intake-service/schemas.py
    Demographics for how a row gets both. Structured fields are None for
    patients created before the Week 6 UI update or by any caller that only
    ever sent the legacy combined fields.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    mrn: str | None = None
    name: str  # composed/legacy display value
    first_name: str | None = None
    last_name: str | None = None
    dob: str | None = None
    ssn: str | None = None
    gender: str | None = None
    address: str | None = None  # composed/legacy display value
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    phone: str | None = None
    email: str | None = None
    notes: str | None = None
    # True when `notes` was withheld because the caller's role may not read
    # clinical notes (client decision 2026-08-14). Distinct from notes being
    # genuinely empty: a UI can say "withheld" rather than imply there are none.
    notes_withheld: bool = False
    created_via: str | None = None
    created_at: datetime | None = None


class PatientPage(BaseModel):
    items: list[PatientSummary]
    total: int
    limit: int
    offset: int


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    encounter_id: int
    patient_id: int
    kind: str | None = None
    title: str | None = None
    body: str | None = None
    status: str | None = None
    reference_range: str | None = None


class EncounterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    encounter_type: str | None = None
    provider: str | None = None
    reason: str | None = None
    location: str | None = None
    status: str | None = None
    summary: str | None = None
    allergies: str | None = None
    medications: str | None = None


class EncounterWithRecords(BaseModel):
    encounter: EncounterOut
    records: list[RecordOut]


class PatientChart(BaseModel):
    patient_id: int
    encounters: list[EncounterWithRecords]


class RecordSearchHit(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    kind: str | None = None
    title: str | None = None
    body: str | None = None


# --- Stage 2 (Week 6) — records reconciliation ("possible duplicate patient")
# view, GET /patients/{id}/reconciliation. Read-only evidence for a clinician;
# see app.py::get_patient_reconciliation for the authorization/audit sequence
# and app.py::_find_ssn_matches for the exact-SSN-only match definition this
# was scoped to (adr/0004 / RIV-160). Never carries a raw ssn.


class IdentitySignal(BaseModel):
    signal_type: str  # "ssn_exact_match" — the only signal this slice computes
    masked_value: str  # e.g. "•••-••-9981" — last 4 digits only, never the full ssn


class ReconciliationSourceRecord(BaseModel):
    patient_id: int
    is_requested_patient: bool
    source_label: str  # "Current chart" | "Possible match"
    name_on_file: str
    dob: str | None = None
    allergies: list[str] = []
    medications: list[str] = []


class ReconciliationDiscrepancy(BaseModel):
    category: str  # "allergy" | "medication"
    value: str
    present_on_patient_ids: list[int]
    missing_on_patient_ids: list[int]
    evidence_ids: list[str]
    review_required: bool = True


class ReconciliationResult(BaseModel):
    patient_id: int
    identity_signals: list[IdentitySignal]
    source_records: list[ReconciliationSourceRecord]
    discrepancies: list[ReconciliationDiscrepancy]
    limitations: list[str]
    escalation: bool
    correlation_id: str
