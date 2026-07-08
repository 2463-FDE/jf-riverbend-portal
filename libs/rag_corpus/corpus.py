"""Deterministic, capped corpus builder for the retrieval-eval harness.

Reads ONLY the checked-in teaching fixtures (db/seed/patients.csv,
db/seed/encounters.csv) — never the client's raw patients/encounters export,
and never queries a live database. This keeps the eval corpus small,
reproducible, and safe to run in CI or on a laptop with no live stack.

These fixtures are the same ones `db/seed/generate_seed.py` preserves
verbatim (Maria Gonzalez as three separate patient rows, etc.) — reusing
them here does not introduce any new PHI-like sample data.

Widening the corpus source (e.g. to the full generated db/seed/seed.sql
bulk, or a live Postgres read) is future work, not in scope for this
deliverable — see docs/planning/retrieval-eval-seam-map-07-08-2026.md.
"""
import csv
import os
from dataclasses import dataclass
from typing import Dict, List

from libs.safe_logging import get_safe_logger

log = get_safe_logger(__name__)

_SEED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "db", "seed")
_PATIENTS_CSV = os.path.join(_SEED_DIR, "patients.csv")
_ENCOUNTERS_CSV = os.path.join(_SEED_DIR, "encounters.csv")


@dataclass(frozen=True)
class CorpusRecord:
    record_id: str
    patient_id: int
    patient_name: str
    text: str
    occurred_at: str


def _load_patient_names(path: str) -> Dict[int, str]:
    with open(path, newline="", encoding="utf-8") as fh:
        return {int(row["id"]): row["name"] for row in csv.DictReader(fh)}


def _encounter_text(patient_name: str, row: Dict[str, str]) -> str:
    allergies = row["allergies"].strip() or "none documented"
    medications = row["medications"].strip() or "none documented"
    return (
        f"{patient_name}: {row['encounter_type']} with {row['provider']} on "
        f"{row['occurred_at']}. {row['summary']} "
        f"Allergies: {allergies}. Medications: {medications}."
    )


def build_corpus(max_records: int) -> List[CorpusRecord]:
    patient_names = _load_patient_names(_PATIENTS_CSV)

    with open(_ENCOUNTERS_CSV, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    capped_rows = rows[:max_records]
    records = []
    for i, row in enumerate(capped_rows, start=1):
        patient_id = int(row["patient_id"])
        patient_name = patient_names.get(patient_id, "Unknown patient")
        records.append(
            CorpusRecord(
                record_id=f"seed-enc-{i:04d}",
                patient_id=patient_id,
                patient_name=patient_name,
                text=_encounter_text(patient_name, row),
                occurred_at=row["occurred_at"],
            )
        )

    log.info(
        "rag_corpus built (total_available=%s, returned=%s, capped=%s)",
        len(rows),
        len(records),
        len(rows) > max_records,
    )
    return records
