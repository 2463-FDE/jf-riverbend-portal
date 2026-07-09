"""Reads structured clinical fields directly from `db/seed/encounters.csv`
for the fragment-coverage-gap metric — independent of `libs/rag_corpus`'s
free-text `CorpusRecord.text`, which is generated prose and not meant to be
parsed back into fields. This reads the same source file Commit 2's corpus
builder reads; it does not modify or duplicate that module's corpus-building
logic.
"""
import csv
import os
from typing import Literal

_ENCOUNTERS_CSV = os.path.join(os.path.dirname(__file__), "..", "..", "db", "seed", "encounters.csv")
ClinicalDomain = Literal["allergy_medication", "lab", "unknown"]


def infer_clinical_domain(query: str, expected_answer: str = "") -> ClinicalDomain:
    """Infer the gold case's clinical domain from its query/answer text.

    The current gold-set schema has no explicit domain field, so this keeps
    the inference deliberately small and transparent. Unknown domains do not
    trigger fragment gaps.
    """
    text = f"{query} {expected_answer}".lower()
    if any(term in text for term in ("allerg", "medication", "medications", "meds", "prescription")):
        return "allergy_medication"
    if any(term in text for term in ("lab", "labs", "cbc", "result", "results", "panel")):
        return "lab"
    return "unknown"


def has_relevant_clinical_content(
    patient_id: int,
    domain: ClinicalDomain = "allergy_medication",
    path: str = _ENCOUNTERS_CSV,
) -> bool:
    """True if this patient has sibling content relevant to the gold case's
    clinical domain.

    Allergy/medication cases count non-blank allergy/medication fields.
    Lab cases count lab encounters or lab-result-like summaries only, so
    allergy/medication rows cannot create a lab fragment gap.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if int(row["patient_id"]) != patient_id:
                continue
            encounter_type = row["encounter_type"].strip().lower()
            summary = row["summary"].strip().lower()
            allergies = row["allergies"].strip().lower()
            medications = row["medications"].strip().lower()
            if domain == "allergy_medication":
                if allergies and allergies != "none known":
                    return True
                if medications:
                    return True
            elif domain == "lab":
                if encounter_type == "lab":
                    return True
                if any(term in summary for term in ("lab", "cbc", "panel", "result")):
                    return True
    return False
