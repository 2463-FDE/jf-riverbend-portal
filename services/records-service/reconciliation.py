"""Stage 2 (Week 6) — records reconciliation ("possible duplicate patient")
view. Exact-SSN candidate matching plus a free-text allergy/medication
discrepancy comparison across the matched charts, for
GET /patients/{id}/reconciliation in app.py.

Scope, per the approved gate (w6-ui-update-records skill, Stage 2):
  - Match definition: exact SSN match only (normalized digits compare),
    excluding the requested patient. No DOB/name matching tier.
  - Clinical data source: encounters.allergies/medications free text
    (comma-separated, per db/schema.sql's own column comments) — not a coded
    or verified allergy/medication list.
  - Read-only. Never merges/writes patient data, never returns a raw ssn.

adr/0004 (RIV-160/AUD-09) proposes the same exact/partial match-key tiering
for the INTAKE (write) path; services/intake-service/app.py::_find_match_candidates
is that sibling implementation. This module only reuses its exact-match
half, for the READ path, on already-existing (possibly long-fragmented)
patients — the historical-duplicate reconciliation adr/0004 itself flags as
"a separate... effort... not scoped" by that ADR. No shared lib exists
between services (ADR 0001), so this is a fresh, intentionally small port
rather than an import.
"""
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Encounter, Patient
from schemas import (
    IdentitySignal,
    ReconciliationDiscrepancy,
    ReconciliationResult,
    ReconciliationSourceRecord,
)

_LIMITATIONS = [
    "This is an exact-SSN candidate signal only, not confirmed proof of one "
    "identity — a clinician must confirm before treating these as the same "
    "patient.",
    "Allergy and medication values come from free-text encounter fields, "
    "not a coded or verified list.",
    "HL7-sourced allergy/medication data (AL1/RXA segments) is not included "
    "here — see the interop-service parser gap.",
]


def _normalize_ssn(ssn: Optional[str]) -> Optional[str]:
    """Digits only, so "412-55-9981" and "412559981" compare equal. Mirrors
    services/intake-service/app.py::_normalize_ssn (ADR 0001: no shared lib,
    so copied rather than imported)."""
    if not ssn:
        return None
    digits = re.sub(r"\D", "", ssn)
    return digits or None


def find_ssn_matches(db: Session, patient_id: int, ssn: Optional[str]) -> list[Patient]:
    """Every OTHER patient whose normalized ssn exactly matches. Full-table
    scan + Python-side compare, same acceptable-at-seed-scale, non-indexed
    approach as intake-service's _find_match_candidates — flagged there as
    not scaling to real production volume without a normalized, indexed
    column; same caveat applies here."""
    normalized = _normalize_ssn(ssn)
    if not normalized:
        return []
    rows = db.execute(select(Patient).where(Patient.ssn.isnot(None))).scalars().all()
    return [row for row in rows if row.id != patient_id and _normalize_ssn(row.ssn) == normalized]


_NO_KNOWN_VALUE_PHRASES = {
    "none",
    "none known",
    "none reported",
    "none recorded",
    "no known allergies",
    "nka",
    "n/a",
    "not applicable",
}


def _split_free_text(value: Optional[str]) -> list[str]:
    """"penicillin, latex" -> ["penicillin", "latex"]. Trims blanks and drops
    "no known allergy"-style negation phrases the seed generator sometimes
    writes literally into this free-text column instead of leaving it blank
    (e.g. "none known") — comparing a negation phrase against a real
    allergen name as if both were candidate values would itself be
    misleading, not just incomplete. Does not dedupe here (caller dedupes
    case-insensitively while preserving the first-seen display casing)."""
    if not value:
        return []
    return [
        part.strip()
        for part in value.split(",")
        if part.strip() and part.strip().lower() not in _NO_KNOWN_VALUE_PHRASES
    ]


def _collect_patient_fields(db: Session, patient_id: int) -> dict[str, dict[str, tuple[str, list[int]]]]:
    """One query per patient, both categories together: {category: {value_lower: (display_value, [encounter_id, ...])}}."""
    encounters = db.execute(select(Encounter).where(Encounter.patient_id == patient_id)).scalars().all()
    result: dict[str, dict[str, tuple[str, list[int]]]] = {"allergy": {}, "medication": {}}
    for enc in encounters:
        for category, field in (("allergy", "allergies"), ("medication", "medications")):
            for item in _split_free_text(getattr(enc, field)):
                key = item.lower()
                display, encounter_ids = result[category].get(key, (item, []))
                encounter_ids.append(enc.id)
                result[category][key] = (display, encounter_ids)
    return result


def build_reconciliation_result(
    db: Session, patient_id: int, requested: Patient, correlation_id: str
) -> ReconciliationResult:
    matches = find_ssn_matches(db, patient_id, requested.ssn)
    all_patients = [requested, *matches]
    fields_by_patient = {p.id: _collect_patient_fields(db, p.id) for p in all_patients}

    source_records = [
        ReconciliationSourceRecord(
            patient_id=p.id,
            is_requested_patient=(p.id == patient_id),
            source_label="Current chart" if p.id == patient_id else "Possible match",
            name_on_file=p.name,
            dob=p.dob,
            allergies=[display for display, _ in fields_by_patient[p.id]["allergy"].values()],
            medications=[display for display, _ in fields_by_patient[p.id]["medication"].values()],
        )
        for p in all_patients
    ]

    discrepancies: list[ReconciliationDiscrepancy] = []
    for category in ("allergy", "medication"):
        all_keys: set[str] = set()
        for p in all_patients:
            all_keys.update(fields_by_patient[p.id][category].keys())
        for key in sorted(all_keys):
            present_ids: list[int] = []
            missing_ids: list[int] = []
            evidence_ids: list[str] = []
            display_value = key
            for p in all_patients:
                entry = fields_by_patient[p.id][category].get(key)
                if entry is None:
                    missing_ids.append(p.id)
                    continue
                display_value, encounter_ids = entry
                present_ids.append(p.id)
                evidence_ids.append(f"PATIENT:{p.id}")
                evidence_ids.extend(f"ENCOUNTER:{eid}" for eid in encounter_ids)
            if present_ids and missing_ids:
                discrepancies.append(
                    ReconciliationDiscrepancy(
                        category=category,
                        value=display_value,
                        present_on_patient_ids=present_ids,
                        missing_on_patient_ids=missing_ids,
                        evidence_ids=evidence_ids,
                        review_required=True,
                    )
                )

    identity_signals: list[IdentitySignal] = []
    if matches:
        normalized = _normalize_ssn(requested.ssn) or ""
        identity_signals.append(
            IdentitySignal(signal_type="ssn_exact_match", masked_value=f"•••-••-{normalized[-4:]}")
        )

    return ReconciliationResult(
        patient_id=patient_id,
        identity_signals=identity_signals,
        source_records=source_records,
        discrepancies=discrepancies,
        limitations=list(_LIMITATIONS),
        escalation=bool(matches),
        correlation_id=correlation_id,
    )
