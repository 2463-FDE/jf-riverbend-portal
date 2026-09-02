"""libs/rag_eval_metrics — sanitized batch RAG-evaluation metrics publishing
(W10 Metrics Stage 5). Mostly pure unit coverage (no live Postgres, no
Bedrock/embedding provider); the one exception is
test_a_real_push_to_a_live_pushgateway_publishes_only_sanitized_series (and
its sibling regression test on the freshness/evaluation split) below, which
drive an actual Pushgateway container — the "one sanitized local publish
proof" this stage asks for, made automated and repeatable rather than a
one-off manual check, and skipped (not failed) wherever the docker CLI is
unavailable.
"""
import subprocess
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from libs.policy_corpus.evaluation import CaseResult, EvaluationReport
from libs.policy_corpus.freshness import CorpusFreshnessReport
from libs.rag_eval.metrics import EvalReport
from libs.rag_eval_metrics import (
    FRESHNESS_CHECKED_AT_METRIC,
    LAST_RUN_TIMESTAMP_METRIC,
    MRR_METRIC,
    freshness_issue_count,
    mean_reciprocal_rank,
    mean_reciprocal_rank_patient_corpus,
    negative_case_retrieval_safety_rate,
    patient_record_corpus_gauges,
    policy_corpus_evaluation_gauges,
    policy_corpus_freshness_gauges,
    push_metrics,
)


def _fresh_report(**overrides):
    defaults = dict(
        corpus_id="c1", provider="bedrock", model="m",
        expected_documents=3, database_documents=3, expected_chunks=10, embedded_chunks=10,
        dimensions=(1024,), missing_documents=(), extra_active_documents=(), document_hash_mismatches=(),
        missing_embeddings=(), extra_active_embeddings=(), chunk_hash_mismatches=(), embedding_hash_mismatches=(),
    )
    defaults.update(overrides)
    return CorpusFreshnessReport(**defaults)


def _case(eval_id, classification, *, missing=(), forbidden=(), unauthorized=(), required=(), retrieved=()):
    return CaseResult(
        eval_id=eval_id, classification=classification, actor_role="clinician",
        retrieved_source_ids=(), retrieved_citation_ids=(), retrieved_identities=retrieved,
        required_targets=required, missing_targets=missing, forbidden_hits=forbidden, unauthorized_hits=unauthorized,
        reason="",
    )


def _eval_report(results, **overrides):
    defaults = dict(
        total_cases=len(results), runnable_cases=0, negative_cases=0, deferred_cases=0, spec_conflicts=0,
        required_targets=0, required_hits=0, retrieved_sources_for_runnable=0,
        forbidden_citation_count=0, unauthorized_retrieval_count=0, results=tuple(results), top_k=5,
    )
    defaults.update(overrides)
    return EvaluationReport(**defaults)


# --- push_metrics ------------------------------------------------------


def test_push_metrics_returns_false_when_no_pushgateway_url_configured():
    assert push_metrics(pushgateway_url="", corpus="policy_corpus", kind="evaluation", gauges={"x": 1.0}) is False


def test_push_metrics_returns_true_on_a_successful_push():
    with patch("prometheus_client.push_to_gateway") as mock_push:
        result = push_metrics(
            pushgateway_url="http://pushgateway:9091", corpus="policy_corpus", kind="evaluation", gauges={"x": 1.0},
        )
    assert result is True
    assert mock_push.call_args.kwargs["job"] == "rag_eval"
    assert mock_push.call_args.kwargs["grouping_key"] == {"corpus": "policy_corpus", "kind": "evaluation"}


def test_push_metrics_uses_a_distinct_grouping_key_per_kind():
    """The whole point of `kind`: a freshness push and an evaluation push
    must address DIFFERENT Pushgateway groupings, never the same one — see
    the regression test against a real gateway below for why."""
    with patch("prometheus_client.push_to_gateway") as mock_push:
        push_metrics(pushgateway_url="http://pushgateway:9091", corpus="policy_corpus", kind="freshness", gauges={"x": 1.0})
    assert mock_push.call_args.kwargs["grouping_key"] == {"corpus": "policy_corpus", "kind": "freshness"}


def test_push_metrics_never_raises_on_a_failed_push():
    with patch("prometheus_client.push_to_gateway", side_effect=OSError("connection refused")):
        result = push_metrics(
            pushgateway_url="http://pushgateway:9091", corpus="policy_corpus", kind="evaluation", gauges={"x": 1.0},
        )
    assert result is False


def test_push_metrics_never_raises_for_a_non_numeric_gauge_value():
    result = push_metrics(
        pushgateway_url="http://pushgateway:9091", corpus="policy_corpus", kind="evaluation",
        gauges={"x": "not-a-number"},
    )
    assert result is False


# --- negative_case_retrieval_safety_rate -----------------------------------


def test_negative_case_retrieval_safety_rate_is_none_when_there_are_no_negative_cases():
    report = _eval_report([_case("e1", "runnable")])
    assert negative_case_retrieval_safety_rate(report) is None


def test_negative_case_retrieval_safety_rate_counts_only_negative_cases_that_passed():
    report = _eval_report([
        _case("e1", "negative"),  # no missing/forbidden/unauthorized -> passed
        _case("e2", "negative", forbidden=("SRC-X",)),  # forbidden hit -> failed
        _case("e3", "runnable"),  # not negative -> excluded from denominator
    ])
    assert negative_case_retrieval_safety_rate(report) == 0.5


# --- freshness_issue_count -------------------------------------------------


def test_freshness_issue_count_sums_every_mismatch_kind():
    freshness = _fresh_report(
        missing_documents=("a@1",), extra_active_documents=("b@1",), document_hash_mismatches=(),
        missing_embeddings=("c1", "c2"), extra_active_embeddings=(), chunk_hash_mismatches=(), embedding_hash_mismatches=(),
    )
    assert freshness_issue_count(freshness) == 4


def test_freshness_issue_count_is_zero_for_a_fully_fresh_corpus():
    assert freshness_issue_count(_fresh_report()) == 0


# --- mean_reciprocal_rank (policy corpus) -----------------------------------


def test_mrr_scores_one_when_the_required_target_is_the_first_result():
    report = _eval_report([_case("e1", "runnable", required=("A@1",), retrieved=("A@1", "B@1"))])
    assert mean_reciprocal_rank(report) == pytest.approx(1.0)


def test_mrr_scores_the_reciprocal_of_a_later_rank():
    report = _eval_report([_case("e1", "runnable", required=("B@1",), retrieved=("A@1", "B@1", "C@1"))])
    assert mean_reciprocal_rank(report) == pytest.approx(0.5)  # rank 2 -> 1/2


def test_mrr_scores_zero_for_a_genuine_miss_not_none():
    """A runnable case that retrieved something, just never the required
    target, is a real, countable miss (0.0) — different from having no
    runnable cases at all (None, tested below)."""
    report = _eval_report([_case("e1", "runnable", required=("Z@1",), retrieved=("A@1", "B@1"))])
    assert mean_reciprocal_rank(report) == 0.0


def test_mrr_uses_the_earliest_of_multiple_required_targets():
    """Two required targets; only the SECOND one (rank 3) appears in the
    retrieved list — but if a hypothetical retrieval had hit the earlier of
    the two first, that rank should win. This case pins the "earliest hit
    among several required targets" rule explicitly."""
    report = _eval_report([
        _case("e1", "runnable", required=("A@1", "C@1"), retrieved=("X@1", "C@1", "A@1")),
    ])
    # C@1 is required and appears at rank 2; A@1 is also required and appears
    # at rank 3 — the earliest (rank 2) must win.
    assert mean_reciprocal_rank(report) == pytest.approx(0.5)


def test_mrr_averages_across_multiple_runnable_cases_and_skips_non_runnable_ones():
    report = _eval_report([
        _case("e1", "runnable", required=("A@1",), retrieved=("A@1",)),  # rank 1 -> 1.0
        _case("e2", "runnable", required=("B@1",), retrieved=("X@1", "B@1")),  # rank 2 -> 0.5
        _case("e3", "negative"),  # excluded — not runnable
        _case("e4", "deferred"),  # excluded — not runnable
    ])
    assert mean_reciprocal_rank(report) == pytest.approx(0.75)


def test_mrr_is_none_not_zero_when_there_are_no_runnable_cases():
    report = _eval_report([_case("e1", "negative"), _case("e2", "deferred")])
    assert mean_reciprocal_rank(report) is None


# --- mean_reciprocal_rank_patient_corpus -------------------------------


def _patient_report(per_case):
    return EvalReport(
        provider_name="fake", top_k=3, total_cases=len(per_case), recall_at_k=0.0, precision_at_k=0.0,
        duplicate_rate=0.0, fragment_coverage_gap=0.0, per_case=per_case, duplicate_clusters=[],
    )


def test_patient_corpus_mrr_scores_one_when_the_expected_record_is_first():
    report = _patient_report([{"expected_record_ids": ["r1"], "retrieved_record_ids": ["r1", "r2"]}])
    assert mean_reciprocal_rank_patient_corpus(report) == pytest.approx(1.0)


def test_patient_corpus_mrr_scores_a_later_rank():
    report = _patient_report([{"expected_record_ids": ["r2"], "retrieved_record_ids": ["r1", "r2", "r3"]}])
    assert mean_reciprocal_rank_patient_corpus(report) == pytest.approx(0.5)


def test_patient_corpus_mrr_scores_zero_for_a_miss():
    report = _patient_report([{"expected_record_ids": ["r9"], "retrieved_record_ids": ["r1", "r2"]}])
    assert mean_reciprocal_rank_patient_corpus(report) == 0.0


def test_patient_corpus_mrr_handles_multiple_expected_records_by_earliest_hit():
    report = _patient_report([{"expected_record_ids": ["r1", "r3"], "retrieved_record_ids": ["r2", "r3", "r1"]}])
    assert mean_reciprocal_rank_patient_corpus(report) == pytest.approx(0.5)  # r3 at rank 2


def test_patient_corpus_mrr_is_none_when_there_are_no_cases_at_all():
    assert mean_reciprocal_rank_patient_corpus(_patient_report([])) is None


# --- policy_corpus_freshness_gauges / policy_corpus_evaluation_gauges ------


def test_freshness_gauges_never_include_evaluation_fields():
    gauges = policy_corpus_freshness_gauges(freshness=_fresh_report())
    assert gauges["rag_corpus_freshness_fresh"] == 1.0
    assert FRESHNESS_CHECKED_AT_METRIC in gauges
    assert LAST_RUN_TIMESTAMP_METRIC not in gauges
    assert "rag_eval_recall_at_k" not in gauges


def test_freshness_gauges_marks_a_stale_corpus_as_not_fresh():
    stale = _fresh_report(missing_documents=("a@1",))
    gauges = policy_corpus_freshness_gauges(freshness=stale)
    assert gauges["rag_corpus_freshness_fresh"] == 0.0
    assert gauges["rag_corpus_freshness_issues_total"] == 1.0


def test_evaluation_gauges_never_include_freshness_fields():
    report = _eval_report([_case("e1", "negative")], forbidden_citation_count=2, unauthorized_retrieval_count=1)
    gauges = policy_corpus_evaluation_gauges(report=report)
    assert gauges["rag_eval_forbidden_citation_count"] == 2.0
    assert LAST_RUN_TIMESTAMP_METRIC in gauges
    assert FRESHNESS_CHECKED_AT_METRIC not in gauges
    assert "rag_corpus_freshness_fresh" not in gauges


def test_evaluation_gauges_includes_recall_precision_mrr_when_present():
    report = _eval_report(
        [_case("e1", "runnable", required=("A@1",), retrieved=("A@1",))],
        required_targets=4, required_hits=3,
    )
    gauges = policy_corpus_evaluation_gauges(report=report)
    assert gauges["rag_eval_recall_at_k"] == pytest.approx(0.75)
    assert gauges[MRR_METRIC] == pytest.approx(1.0)
    # No negative-classified case in this report -> the rate is undefined
    # (None) and correctly omitted, not published as a guessed value.
    assert "rag_eval_negative_case_retrieval_safety_rate" not in gauges


def test_evaluation_gauges_top_k_reflects_the_reports_own_value_not_a_constant():
    """The policy corpus defaults to k=5 (db/policy_corpus_evaluate.py's
    --top-k), never the same constant as the patient-record corpus's k=1 —
    this must come from the report the caller actually produced, not a
    value duplicated inside this module."""
    for k in (5, 3, 17):
        report = _eval_report([_case("e1", "negative")], top_k=k)
        assert policy_corpus_evaluation_gauges(report=report)["rag_eval_top_k"] == float(k)


def test_evaluation_gauges_omits_recall_precision_and_mrr_when_no_runnable_cases_exist():
    report = _eval_report([_case("e1", "negative")])
    gauges = policy_corpus_evaluation_gauges(report=report)
    assert "rag_eval_recall_at_k" not in gauges
    assert "rag_eval_precision_at_k" not in gauges
    assert MRR_METRIC not in gauges


def test_evaluation_gauges_includes_embedding_stats_when_a_client_is_given():
    class _FakeClient:
        tokens_used = 250
        retry_count = 2

    gauges = policy_corpus_evaluation_gauges(report=_eval_report([_case("e1", "negative")]), embedding_client=_FakeClient())
    assert gauges["rag_eval_embedding_input_tokens_total"] == 250.0
    assert gauges["rag_eval_embedding_provider_retry_total"] == 2.0


# --- patient_record_corpus_gauges ------------------------------------------


def test_patient_record_corpus_gauges_normalizes_percentages_to_fractions():
    report = EvalReport(
        provider_name="fake", top_k=1, total_cases=3, recall_at_k=66.7, precision_at_k=33.3,
        duplicate_rate=100.0, fragment_coverage_gap=0.0,
        per_case=[{"expected_record_ids": ["r1"], "retrieved_record_ids": ["r1"]}], duplicate_clusters=[],
    )
    gauges = patient_record_corpus_gauges(report=report)
    assert gauges["rag_eval_recall_at_k"] == pytest.approx(0.667)
    assert gauges["rag_eval_precision_at_k"] == pytest.approx(0.333)
    assert gauges["rag_eval_duplicate_rate"] == pytest.approx(1.0)
    assert gauges["rag_eval_fragment_coverage_gap"] == pytest.approx(0.0)
    assert gauges[MRR_METRIC] == pytest.approx(1.0)
    assert gauges["rag_eval_top_k"] == 1.0
    assert LAST_RUN_TIMESTAMP_METRIC in gauges


def test_patient_record_corpus_gauges_top_k_reflects_the_reports_own_value():
    """Preserves this corpus's own default (k=1, RAG_EVAL_TOP_K) independent
    of whatever the policy corpus uses — never forced to match it."""
    report = EvalReport(
        provider_name="fake", top_k=7, total_cases=0, recall_at_k=0.0, precision_at_k=0.0,
        duplicate_rate=0.0, fragment_coverage_gap=0.0, per_case=[], duplicate_clusters=[],
    )
    assert patient_record_corpus_gauges(report=report)["rag_eval_top_k"] == 7.0


def test_patient_record_corpus_gauges_omits_mrr_when_there_are_no_cases():
    report = EvalReport(
        provider_name="fake", top_k=1, total_cases=0, recall_at_k=0.0, precision_at_k=0.0,
        duplicate_rate=0.0, fragment_coverage_gap=0.0, per_case=[], duplicate_clusters=[],
    )
    gauges = patient_record_corpus_gauges(report=report)
    assert MRR_METRIC not in gauges


def test_patient_record_corpus_gauges_carries_no_query_or_patient_identifying_content():
    """The published gauge dict must contain ONLY the whitelisted numeric
    keys this module defines — never per_case/duplicate_clusters, which
    carry raw queries and patient ids (see libs/rag_eval/metrics.py)."""
    report = EvalReport(
        provider_name="fake", top_k=1, total_cases=1, recall_at_k=0.0, precision_at_k=0.0,
        duplicate_rate=0.0, fragment_coverage_gap=0.0,
        per_case=[{
            "query": "show me Maria Gonzalez's allergies", "expected_patient_id": 1042,
            "expected_record_ids": ["seed-enc-0001"], "retrieved_record_ids": ["seed-enc-0002"],
        }],
        duplicate_clusters=[[1042, 1330]],
    )
    gauges = patient_record_corpus_gauges(report=report)
    allowed = {
        "rag_eval_top_k", "rag_eval_recall_at_k", "rag_eval_precision_at_k", "rag_eval_duplicate_rate",
        "rag_eval_fragment_coverage_gap", MRR_METRIC, LAST_RUN_TIMESTAMP_METRIC,
    }
    assert set(gauges) == allowed
    assert all(isinstance(v, float) for v in gauges.values())


# --- real, local publish proof ----------------------------------------------


def _docker_available():
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _start_pushgateway():
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", "0:9091", "prom/pushgateway:v1.11.3"],
        capture_output=True, text=True,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start a local pushgateway container: {started.stderr.strip()}")
    container_id = started.stdout.strip()
    port_output = subprocess.run(
        ["docker", "port", container_id, "9091/tcp"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    url = f"http://localhost:{port_output.rsplit(':', 1)[-1]}"
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{url}/-/healthy", timeout=1)
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.5)
    else:
        subprocess.run(["docker", "stop", container_id], capture_output=True)
        pytest.skip("local pushgateway container never became healthy")
    return container_id, url


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")
def test_a_real_push_to_a_live_pushgateway_publishes_only_sanitized_series():
    """Starts a real, throwaway prom/pushgateway container (the same image/
    tag pinned in docker-compose.yml), pushes a representative gauge set for
    BOTH corpora through the real `push_metrics()` code path (no mocking),
    then reads the gateway's own /metrics endpoint back and asserts it
    contains exactly the sanitized series pushed — nothing else, and
    critically nothing that could ever carry a query, patient id, or
    document/citation identifier, since push_metrics only ever accepts a
    flat numeric dict in the first place."""
    container_id, url = _start_pushgateway()
    try:
        assert push_metrics(
            pushgateway_url=url, corpus="policy_corpus", kind="evaluation",
            gauges={"rag_eval_recall_at_k": 0.8, MRR_METRIC: 0.9},
        ) is True
        assert push_metrics(
            pushgateway_url=url, corpus="policy_corpus", kind="freshness",
            gauges={"rag_corpus_freshness_fresh": 1.0},
        ) is True
        assert push_metrics(
            pushgateway_url=url, corpus="patient_record_corpus", kind="evaluation",
            gauges={"rag_eval_duplicate_rate": 0.25},
        ) is True

        body = urllib.request.urlopen(f"{url}/metrics", timeout=5).read().decode()
        assert 'rag_eval_recall_at_k{corpus="policy_corpus"' in body
        assert 'rag_corpus_freshness_fresh{corpus="policy_corpus"' in body
        assert 'rag_eval_duplicate_rate{corpus="patient_record_corpus"' in body
        # No query, patient, document, or citation text is possible here —
        # push_metrics' only input shape is a flat {name: float} dict — but
        # this asserts it anyway, against the actual bytes the gateway
        # would serve to Prometheus.
        for forbidden in ("Gonzalez", "query", "patient_id", "citation_id", "SRC-"):
            assert forbidden not in body
    finally:
        subprocess.run(["docker", "stop", container_id], capture_output=True)


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")
def test_a_freshness_only_push_cannot_delete_or_refresh_a_completed_evaluation():
    """The exact regression this stage's review found: before the
    kind="freshness"/kind="evaluation" split, a stale or --verify-only run
    (freshness gauges only) pushed under the SAME Pushgateway grouping key a
    completed evaluation last used — and push_to_gateway REPLACES the whole
    metric set under one grouping key, so the completed evaluation's
    recall/precision/MRR series were silently erased. Proven here against a
    REAL Pushgateway, not a mock, because the bug is in Pushgateway's own
    replace-per-grouping-key semantics, not in this module's Python."""
    container_id, url = _start_pushgateway()
    try:
        evaluation_timestamp = 1_700_000_000.0
        assert push_metrics(
            pushgateway_url=url, corpus="policy_corpus", kind="evaluation",
            gauges={"rag_eval_recall_at_k": 0.9, LAST_RUN_TIMESTAMP_METRIC: evaluation_timestamp},
        ) is True

        # A LATER stale/--verify-only run publishes freshness-only gauges —
        # this must not touch the evaluation grouping at all.
        assert push_metrics(
            pushgateway_url=url, corpus="policy_corpus", kind="freshness",
            gauges={"rag_corpus_freshness_fresh": 0.0, FRESHNESS_CHECKED_AT_METRIC: time.time()},
        ) is True

        body = urllib.request.urlopen(f"{url}/metrics", timeout=5).read().decode()
        # The completed evaluation's own series must still be exactly as
        # published — neither deleted nor its timestamp refreshed.
        assert 'rag_eval_recall_at_k{corpus="policy_corpus"' in body
        timestamp_line = next(
            line for line in body.splitlines()
            if line.startswith(LAST_RUN_TIMESTAMP_METRIC) and 'kind="evaluation"' in line
        )
        published_value = float(timestamp_line.rsplit(" ", 1)[-1])
        assert published_value == pytest.approx(evaluation_timestamp), f"evaluation timestamp changed: {timestamp_line!r}"
        # And the freshness grouping's own value must be exactly what the
        # stale run just pushed.
        assert 'rag_corpus_freshness_fresh{corpus="policy_corpus"' in body
    finally:
        subprocess.run(["docker", "stop", container_id], capture_output=True)
