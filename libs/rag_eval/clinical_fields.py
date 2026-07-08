"""Reads structured allergy/medication fields directly from
`db/seed/encounters.csv` for the fragment-coverage-gap metric — independent
of `libs/rag_corpus`'s free-text `CorpusRecord.text`, which is generated
prose and not meant to be parsed back into fields. This reads the same
source file Commit 2's corpus builder reads; it does not modify or duplicate
that module's corpus-building logic.
"""
import csv
import os

_ENCOUNTERS_CSV = os.path.join(os.path.dirname(__file__), "..", "..", "db", "seed", "encounters.csv")


def has_relevant_clinical_content(patient_id: int, path: str = _ENCOUNTERS_CSV) -> bool:
    """True if any encounter for this patient has a non-blank allergy or
    medication field. "none known"-style explicit negatives don't count —
    only content that could actually change a clinical answer if missed.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if int(row["patient_id"]) != patient_id:
                continue
            allergies = row["allergies"].strip().lower()
            medications = row["medications"].strip().lower()
            if allergies and allergies != "none known":
                return True
            if medications:
                return True
    return False
