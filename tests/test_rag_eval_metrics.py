from libs.rag_corpus import CorpusRecord
from libs.rag_eval.goldset import GoldCase
from libs.rag_eval.metrics import compute_metrics


def _record(record_id, patient_id):
    return CorpusRecord(
        record_id=record_id,
        patient_id=patient_id,
        patient_name="seed fixture",
        text="seed fixture text",
        occurred_at="2026-01-01 00:00:00",
    )


def test_lab_case_does_not_count_allergy_medication_sibling_as_fragment_gap():
    case = GoldCase(
        query="latest lab results for M. Gonzalez",
        expected_patient_id=1588,
        expected_answer="CBC panel within normal limits (2026-05-19).",
        cites_records=[3],
    )
    corpus = [
        _record("seed-enc-0002", 1330),  # sibling with allergy/medication data in seed encounters
        _record("seed-enc-0003", 1588),
        _record("seed-enc-0005", 1601),
    ]

    report = compute_metrics(
        gold_cases=[case],
        corpus=corpus,
        retrieved_by_case={case.query: [_record("seed-enc-0005", 1601)]},
        clusters={1588: [1042, 1330, 1588]},
        top_k=1,
        provider_name="fake",
    )

    assert report.fragment_coverage_gap == 0.0
    assert report.per_case[0]["clinical_domain"] == "lab"
    assert report.per_case[0]["fragment_coverage_gap"] is False
