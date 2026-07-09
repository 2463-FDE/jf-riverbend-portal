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


# --- recall@k / precision@k -------------------------------------------------


def test_recall_and_precision_on_synthetic_gold_set():
    cases = [
        GoldCase(query="q1", expected_patient_id=1, expected_answer="a1", cites_records=[1]),
        GoldCase(query="q2", expected_patient_id=2, expected_answer="a2", cites_records=[2]),
    ]
    corpus = [_record("seed-enc-0001", 1), _record("seed-enc-0002", 2), _record("seed-enc-0003", 3)]
    retrieved_by_case = {
        "q1": [_record("seed-enc-0001", 1)],  # hit: cited record retrieved
        "q2": [_record("seed-enc-0003", 3)],  # miss: cited record not retrieved
    }

    report = compute_metrics(
        gold_cases=cases,
        corpus=corpus,
        retrieved_by_case=retrieved_by_case,
        clusters={},
        top_k=1,
        provider_name="fake",
    )

    assert report.recall_at_k == 50.0
    assert report.precision_at_k == 50.0
    assert report.per_case[0]["recall_hit"] is True
    assert report.per_case[1]["recall_hit"] is False


def test_precision_at_k_counts_only_relevant_records_among_retrieved():
    case = GoldCase(query="q1", expected_patient_id=1, expected_answer="a1", cites_records=[1])
    corpus = [_record("seed-enc-0001", 1), _record("seed-enc-0002", 2)]
    retrieved_by_case = {"q1": [_record("seed-enc-0001", 1), _record("seed-enc-0002", 2)]}

    report = compute_metrics(
        gold_cases=[case],
        corpus=corpus,
        retrieved_by_case=retrieved_by_case,
        clusters={},
        top_k=2,
        provider_name="fake",
    )

    # 1 of the 2 retrieved records is actually cited by the gold case.
    assert report.precision_at_k == 50.0
    assert report.recall_at_k == 100.0


def test_precision_and_recall_are_zero_when_nothing_is_retrieved():
    case = GoldCase(query="q1", expected_patient_id=1, expected_answer="a1", cites_records=[1])

    report = compute_metrics(
        gold_cases=[case],
        corpus=[_record("seed-enc-0001", 1)],
        retrieved_by_case={"q1": []},
        clusters={},
        top_k=1,
        provider_name="fake",
    )

    assert report.precision_at_k == 0.0
    assert report.recall_at_k == 0.0


# --- duplicate-rate ----------------------------------------------------------


def test_duplicate_rate_counts_only_multi_patient_clusters():
    # 1042/1330/1588 share one identity cluster (duplicate); 1601 is a singleton.
    clusters = {1042: [1042, 1330, 1588], 1330: [1042, 1330, 1588], 1588: [1042, 1330, 1588], 1601: [1601]}
    case = GoldCase(query="q1", expected_patient_id=1042, expected_answer="a1", cites_records=[1])

    report = compute_metrics(
        gold_cases=[case],
        corpus=[_record("seed-enc-0001", 1042)],
        retrieved_by_case={"q1": [_record("seed-enc-0001", 1042)]},
        clusters=clusters,
        top_k=1,
        provider_name="fake",
    )

    # 1 duplicate cluster out of 2 distinct clusters ({1042,1330,1588} and {1601}).
    assert report.duplicate_rate == 50.0
    assert report.duplicate_clusters == [[1042, 1330, 1588]]


def test_duplicate_rate_is_zero_when_every_cluster_is_a_singleton():
    clusters = {1: [1], 2: [2]}
    case = GoldCase(query="q1", expected_patient_id=1, expected_answer="a1", cites_records=[1])

    report = compute_metrics(
        gold_cases=[case],
        corpus=[_record("seed-enc-0001", 1)],
        retrieved_by_case={"q1": [_record("seed-enc-0001", 1)]},
        clusters=clusters,
        top_k=1,
        provider_name="fake",
    )

    assert report.duplicate_rate == 0.0
    assert report.duplicate_clusters == []


# --- fragment-coverage gap: case-specific domain logic -----------------------
# compute_metrics() only flags a gap when a sibling patient in the identity
# cluster has content relevant to the gold case's own clinical domain
# (libs.rag_eval.clinical_fields.has_relevant_clinical_content) — this is the
# fix for the bug where any sibling record (e.g. allergy/medication) was
# enough to flag an unrelated (e.g. lab) query as a fragment gap. These tests
# isolate compute_metrics' branching by stubbing the CSV lookup directly.


def test_fragment_gap_is_flagged_when_domain_matching_sibling_is_not_retrieved(monkeypatch):
    case = GoldCase(
        query="known allergies for patient",
        expected_patient_id=1588,
        expected_answer="penicillin allergy",
        cites_records=[3],
    )
    corpus = [_record("seed-enc-0002", 1330), _record("seed-enc-0003", 1588)]

    monkeypatch.setattr(
        "libs.rag_eval.metrics.has_relevant_clinical_content",
        lambda patient_id, domain: patient_id == 1330 and domain == "allergy_medication",
    )

    report = compute_metrics(
        gold_cases=[case],
        corpus=corpus,
        retrieved_by_case={case.query: [_record("seed-enc-0003", 1588)]},  # sibling 1330 not retrieved
        clusters={1588: [1330, 1588]},
        top_k=1,
        provider_name="fake",
    )

    assert report.fragment_coverage_gap == 100.0
    assert report.per_case[0]["fragment_coverage_gap"] is True


def test_fragment_gap_is_not_flagged_when_no_sibling_matches_the_case_domain(monkeypatch):
    case = GoldCase(
        query="latest lab results",
        expected_patient_id=1588,
        expected_answer="CBC panel within normal limits",
        cites_records=[3],
    )
    corpus = [_record("seed-enc-0002", 1330), _record("seed-enc-0003", 1588)]

    # Sibling 1330 may have allergy/medication content, but none relevant to
    # this lab-domain case — the case-specific check must not flag a gap.
    monkeypatch.setattr(
        "libs.rag_eval.metrics.has_relevant_clinical_content",
        lambda patient_id, domain: False,
    )

    report = compute_metrics(
        gold_cases=[case],
        corpus=corpus,
        retrieved_by_case={case.query: [_record("seed-enc-0003", 1588)]},
        clusters={1588: [1330, 1588]},
        top_k=1,
        provider_name="fake",
    )

    assert report.fragment_coverage_gap == 0.0
    assert report.per_case[0]["fragment_coverage_gap"] is False


def test_fragment_gap_is_not_flagged_when_the_matching_sibling_was_retrieved(monkeypatch):
    case = GoldCase(
        query="known allergies for patient",
        expected_patient_id=1588,
        expected_answer="penicillin allergy",
        cites_records=[3],
    )
    corpus = [_record("seed-enc-0002", 1330), _record("seed-enc-0003", 1588)]

    monkeypatch.setattr(
        "libs.rag_eval.metrics.has_relevant_clinical_content",
        lambda patient_id, domain: patient_id == 1330 and domain == "allergy_medication",
    )

    report = compute_metrics(
        gold_cases=[case],
        corpus=corpus,
        # Both the expected patient's record and the matching sibling's record are retrieved.
        retrieved_by_case={case.query: [_record("seed-enc-0003", 1588), _record("seed-enc-0002", 1330)]},
        clusters={1588: [1330, 1588]},
        top_k=2,
        provider_name="fake",
    )

    assert report.fragment_coverage_gap == 0.0


# --- regression: M. Gonzalez lab query vs. allergy/medication sibling --------
# Exercises the real clinical_fields.py lookup against db/seed/encounters.csv
# (not a stub) to guard the specific bug this metric was fixed for.


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
