"""Unit tests for the case-specific clinical-domain lookup
(libs/rag_eval/clinical_fields.py) that libs/rag_eval/metrics.py relies on to
decide whether a sibling patient's content is relevant to a *specific* gold
case's clinical domain — the fix for allergy/medication siblings being
counted as a fragment gap for unrelated lab queries.
"""
import csv

import pytest

from libs.rag_eval.clinical_fields import has_relevant_clinical_content, infer_clinical_domain

_HEADER = ["patient_id", "encounter_type", "provider", "summary", "allergies", "medications", "occurred_at"]


def _write_encounters_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_HEADER)
        writer.writerows(rows)


# --- infer_clinical_domain ---------------------------------------------------


@pytest.mark.parametrize(
    "query,answer,expected",
    [
        ("latest lab results for M. Gonzalez", "CBC panel within normal limits", "lab"),
        ("what are this patient's known allergies", "penicillin allergy", "allergy_medication"),
        ("what medications is the patient on", "lisinopril 10mg", "allergy_medication"),
        ("who is this patient's primary care provider", "Dr. Smith", "unknown"),
    ],
)
def test_infer_clinical_domain(query, answer, expected):
    assert infer_clinical_domain(query, answer) == expected


# --- has_relevant_clinical_content: case-specific domain matching -----------


def test_allergy_medication_row_counts_for_allergy_medication_domain_only(tmp_path):
    csv_path = tmp_path / "encounters.csv"
    _write_encounters_csv(
        csv_path,
        [[1, "office_visit", "Dr. Lee", "routine follow-up", "penicillin", "none", "2026-01-01"]],
    )

    assert has_relevant_clinical_content(1, "allergy_medication", path=str(csv_path)) is True
    assert has_relevant_clinical_content(1, "lab", path=str(csv_path)) is False


def test_lab_encounter_type_counts_for_lab_domain_only(tmp_path):
    csv_path = tmp_path / "encounters.csv"
    _write_encounters_csv(
        csv_path,
        [[1, "lab", "Dr. Lee", "routine panel", "none known", "", "2026-01-01"]],
    )

    assert has_relevant_clinical_content(1, "lab", path=str(csv_path)) is True
    assert has_relevant_clinical_content(1, "allergy_medication", path=str(csv_path)) is False


def test_none_known_allergies_and_blank_medications_do_not_count_as_relevant(tmp_path):
    csv_path = tmp_path / "encounters.csv"
    _write_encounters_csv(
        csv_path,
        [[1, "office_visit", "Dr. Lee", "routine follow-up", "none known", "", "2026-01-01"]],
    )

    assert has_relevant_clinical_content(1, "allergy_medication", path=str(csv_path)) is False


def test_summary_lab_keyword_counts_as_lab_content_even_without_lab_encounter_type(tmp_path):
    csv_path = tmp_path / "encounters.csv"
    _write_encounters_csv(
        csv_path,
        [[1, "office_visit", "Dr. Lee", "reviewed recent CBC panel results", "none known", "", "2026-01-01"]],
    )

    assert has_relevant_clinical_content(1, "lab", path=str(csv_path)) is True


def test_unknown_domain_never_counts_as_relevant_even_with_matching_content(tmp_path):
    csv_path = tmp_path / "encounters.csv"
    _write_encounters_csv(
        csv_path,
        [[1, "lab", "Dr. Lee", "CBC panel", "penicillin", "lisinopril", "2026-01-01"]],
    )

    assert has_relevant_clinical_content(1, "unknown", path=str(csv_path)) is False


def test_only_the_requested_patient_id_rows_are_considered(tmp_path):
    csv_path = tmp_path / "encounters.csv"
    _write_encounters_csv(
        csv_path,
        [
            [1, "office_visit", "Dr. Lee", "routine follow-up", "penicillin", "none", "2026-01-01"],
            [2, "lab", "Dr. Lee", "CBC panel", "none known", "", "2026-01-02"],
        ],
    )

    # Patient 2 has no allergy/medication row (only patient 1 does), but does
    # have a lab row — the case-specific check must key off patient_id, not
    # just "does this CSV contain any matching row at all."
    assert has_relevant_clinical_content(2, "allergy_medication", path=str(csv_path)) is False
    assert has_relevant_clinical_content(2, "lab", path=str(csv_path)) is True
