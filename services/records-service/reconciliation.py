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
from patient_access_gate import authorized_patient_ids
from phi import compute_ssn_blind_index, decrypt_patient_field
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
    """Digits only, so "412-55-9981" and "412559981" compare equal — but
    ONLY when the result is a plausible real SSN. Mirrors services/
    intake-service/app.py::_normalize_ssn (ADR 0001: no shared lib, so
    copied rather than imported), plus a validation floor that file didn't
    need (intake never uses this as a match key across other patients).

    Codex review (2026-08-07, PR #22): a bare digit-strip previously treated
    any non-empty digit string as a valid match key, so a placeholder like
    "000-00-0000" or a partial/mistyped SSN grouped unrelated patients as an
    "exact match," producing false cross-chart allergy/medication
    discrepancy evidence a clinician could act on clinically. Now requires
    exactly 9 digits and rejects the SSA's own documented never-issued
    patterns (area 000/666/900-999, group 00, serial 0000) and any
    single-digit-repeated string (e.g. "000000000"). An invalid-shaped SSN
    is now no different from having no SSN at all: this returns None, so
    find_ssn_matches() can never emit a candidate from it — for either the
    requested patient's own SSN or a database row being compared against."""
    if not ssn:
        return None
    digits = re.sub(r"\D", "", ssn)
    if len(digits) != 9:
        return None
    if len(set(digits)) == 1:  # "000000000", "111111111", ... — never issued
        return None
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in ("000", "666") or area.startswith("9") or group == "00" or serial == "0000":
        return None
    return digits


def find_ssn_match_ids(db: Session, patient_id: int, ssn: Optional[str]) -> list[int]:
    """Every OTHER patient id whose stored ssn_digits exactly matches the
    requested patient's (already-decrypted, plaintext) normalized ssn.

    Codex review (2026-08-08, PR #22 round 5 — medium): this used to read
    EVERY patient's ssn into Python and normalize+compare row by row on every
    reconciliation request — an unbounded scan of unauthorized patients' PHI
    (their SSNs), and a timeout risk at real volume. Migration 015 added
    `patients.ssn_digits`, a database-computed, indexed, digit-only column;
    the query filtered on it directly, so only rows whose stored digits
    already equalled the exact 9-digit key were ever read — at most a
    handful, never a full-table scan.

    w8-planner-2 P2 (adr/0012): migration 031 replaced ssn_digits with an
    HMAC-SHA256 blind index (libs/phi_crypto), since ssn is now
    application-encrypted and a plaintext digit column would defeat that.
    The query below now compares against the blind index of the normalized
    key instead of the raw digits — same indexed-equality shape, same "at
    most a handful of rows read" property, just a different value on both
    sides of the comparison.

    ssn_digits itself is unvalidated (pure digit extraction, then blind-
    indexed — a placeholder like "000-00-0000" indexes as
    compute_ssn_blind_index("000000000")), but that's safe: `_normalize_ssn`
    rejects invalid-shaped SSNs for the QUERY key before this runs (returns
    None, short-circuiting to `[]` below), so an invalid stored value can
    never equal a validated key's blind index either — see that function's
    docstring. The `row.ssn_digits == blind_index` recheck below is
    defense-in-depth (never trust a query result to have actually honored
    its own WHERE clause), not a second scan — `rows` is already the
    narrow, indexed match set, not every patient.

    Still selects only `id`/`ssn_digits` (never name, dob, or any clinical
    field) — an unauthorized candidate's demographic/clinical PHI is never
    loaded into application memory. build_reconciliation_result below
    authorizes these ids BEFORE fetching full Patient detail for any of them
    — see _fetch_patients_by_id."""
    normalized = _normalize_ssn(ssn)
    if not normalized:
        return []
    blind_index = compute_ssn_blind_index(normalized)
    rows = db.execute(
        select(Patient.id, Patient.ssn_digits).where(
            Patient.ssn_digits == blind_index, Patient.id != patient_id
        )
    ).all()
    return [row.id for row in rows if row.id != patient_id and row.ssn_digits == blind_index]


def _fetch_patients_by_id(db: Session, patient_ids: set[int]) -> list[Patient]:
    """Full Patient detail (name, dob, ...) for an already-known, already-
    authorized set of ids only — called with the AUTHORIZED subset of
    find_ssn_match_ids' candidates, never the raw candidate set, so an
    unauthorized candidate's name/dob is never fetched, let alone returned."""
    if not patient_ids:
        return []
    rows = db.execute(select(Patient).where(Patient.id.in_(patient_ids))).scalars().all()
    return sorted(rows, key=lambda p: p.id)


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
    db: Session, patient_id: int, requested: Patient, correlation_id: str, *, actor_id: str
) -> ReconciliationResult:
    """Week 4 catch-up (Codex review, 2026-08-07, PR #22 — high, no-ship):
    a prior version returned every SSN-matched candidate's name, DOB,
    allergies, medications, and discrepancy evidence on the strength of the
    REQUESTED patient's own authorization alone — StaffAccessGate wasn't
    patient-specific, so nothing scoped the extra charts this expanded into.

    Every candidate patient_id is now independently authorized via
    authorized_patient_ids() (the same batch primitive
    services/records-service/app.py::search_records uses) BEFORE any of its
    encounter/allergy/medication data is even queried, let alone returned.
    An unauthorized candidate is discarded completely at that point — it
    never reaches _collect_patient_fields, never becomes a
    ReconciliationSourceRecord, and is not reflected in identity_signals,
    discrepancies, or escalation. The response is indistinguishable from
    that candidate never having existed: no id, name, DOB, allergy,
    medication, evidence id, count, or placeholder attributable to it.

    Round 4 review: authorization now runs on bare candidate ids
    (find_ssn_match_ids), and full Patient detail (name, dob) is only
    fetched afterward for the authorized subset (_fetch_patients_by_id) —
    an unauthorized candidate's demographic PHI is never loaded into
    application memory at all, not just excluded from the response.

    w8-planner-2 P2 (adr/0012): ssn/dob are application-encrypted now.
    `requested.ssn` is decrypted once, up front, into a local — never by
    mutating `requested` itself (see phi.py's docstring on why overwriting
    an ORM object's encrypted-column attribute with plaintext is unsafe:
    a later session.commit() on that object would write the plaintext back
    over the real ciphertext). Each matched candidate's dob is decrypted
    only when building its ReconciliationSourceRecord below, same reason.
    """
    requested_ssn = decrypt_patient_field(patient_id, "ssn", requested.ssn, requested.ssn_key_version)
    candidate_ids = find_ssn_match_ids(db, patient_id, requested_ssn)
    authorized_ids = authorized_patient_ids(db, actor_id, candidate_ids) if candidate_ids else set()
    matches = _fetch_patients_by_id(db, authorized_ids)
    all_patients = [requested, *matches]
    fields_by_patient = {p.id: _collect_patient_fields(db, p.id) for p in all_patients}

    source_records = [
        ReconciliationSourceRecord(
            patient_id=p.id,
            is_requested_patient=(p.id == patient_id),
            source_label="Current chart" if p.id == patient_id else "Possible match",
            name_on_file=p.name,
            dob=decrypt_patient_field(p.id, "dob", p.dob, p.dob_key_version),
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
        normalized = _normalize_ssn(requested_ssn) or ""
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
