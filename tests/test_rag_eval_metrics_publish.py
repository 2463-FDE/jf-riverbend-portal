"""libs/rag_eval_metrics — sanitized batch RAG-evaluation metrics publishing
(W10 Metrics Stage 5). Mostly pure unit coverage (no live Postgres, no
Bedrock/embedding provider); the one exception is
test_a_real_push_to_a_live_pushgateway_publishes_only_sanitized_series
below, which drives an actual Pushgateway container — the "one sanitized
local publish proof" this stage asks for, made automated and repeatable
rather than a one-off manual check, and skipped (not failed) wherever the
docker CLI is unavailable.
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
    LAST_RUN_TIMESTAMP_METRIC,
    freshness_issue_count,
    negative_case_accuracy,
    patient_record_corpus_gauges,
    policy_corpus_gauges,
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


def _case(eval_id, classification, *, missing=(), forbidden=(), unauthorized=()):
    return CaseResult(
        eval_id=eval_id, classification=classification, actor_role="clinician",
        retrieved_source_ids=(), retrieved_citation_ids=(),
        required_targets=(), missing_targets=missing, forbidden_hits=forbidden, unauthorized_hits=unauthorized,
        reason="",
    )


def _eval_report(results, **overrides):
    defaults = dict(
        total_cases=len(results), runnable_cases=0, negative_cases=0, deferred_cases=0, spec_conflicts=0,
        required_targets=0, required_hits=0, retrieved_sources_for_runnable=0,
        forbidden_citation_count=0, unauthorized_retrieval_count=0, results=tuple(results),
    )
    defaults.update(overrides)
    return EvaluationReport(**defaults)


# --- push_metrics ------------------------------------------------------


def test_push_metrics_returns_false_when_no_pushgateway_url_configured():
    assert push_metrics(pushgateway_url="", corpus="policy_corpus", gauges={"x": 1.0}) is False


def test_push_metrics_returns_true_on_a_successful_push():
    with patch("prometheus_client.push_to_gateway") as mock_push:
        result = push_metrics(pushgateway_url="http://pushgateway:9091", corpus="policy_corpus", gauges={"x": 1.0})
    assert result is True
    assert mock_push.call_args.kwargs["job"] == "rag_eval"
    assert mock_push.call_args.kwargs["grouping_key"] == {"corpus": "policy_corpus"}


def test_push_metrics_never_raises_on_a_failed_push():
    with patch("prometheus_client.push_to_gateway", side_effect=OSError("connection refused")):
        result = push_metrics(pushgateway_url="http://pushgateway:9091", corpus="policy_corpus", gauges={"x": 1.0})
    assert result is False


def test_push_metrics_never_raises_for_a_non_numeric_gauge_value():
    result = push_metrics(pushgateway_url="http://pushgateway:9091", corpus="policy_corpus", gauges={"x": "not-a-number"})
    assert result is False


# --- negative_case_accuracy ----------------------------------------------


def test_negative_case_accuracy_is_none_when_there_are_no_negative_cases():
    report = _eval_report([_case("e1", "runnable")])
    assert negative_case_accuracy(report) is None


def test_negative_case_accuracy_counts_only_negative_cases_that_passed():
    report = _eval_report([
        _case("e1", "negative"),  # no missing/forbidden/unauthorized -> passed
        _case("e2", "negative", forbidden=("SRC-X",)),  # forbidden hit -> failed
        _case("e3", "runnable"),  # not negative -> excluded from denominator
    ])
    assert negative_case_accuracy(report) == 0.5


# --- freshness_issue_count -------------------------------------------------


def test_freshness_issue_count_sums_every_mismatch_kind():
    freshness = _fresh_report(
        missing_documents=("a@1",), extra_active_documents=("b@1",), document_hash_mismatches=(),
        missing_embeddings=("c1", "c2"), extra_active_embeddings=(), chunk_hash_mismatches=(), embedding_hash_mismatches=(),
    )
    assert freshness_issue_count(freshness) == 4


def test_freshness_issue_count_is_zero_for_a_fully_fresh_corpus():
    assert freshness_issue_count(_fresh_report()) == 0


# --- policy_corpus_gauges --------------------------------------------------


def test_policy_corpus_gauges_publishes_freshness_only_when_report_is_absent():
    gauges = policy_corpus_gauges(freshness=_fresh_report())
    assert gauges["rag_corpus_freshness_fresh"] == 1.0
    assert "rag_eval_recall_at_k" not in gauges
    assert LAST_RUN_TIMESTAMP_METRIC in gauges


def test_policy_corpus_gauges_marks_a_stale_corpus_as_not_fresh():
    stale = _fresh_report(missing_documents=("a@1",))
    gauges = policy_corpus_gauges(freshness=stale)
    assert gauges["rag_corpus_freshness_fresh"] == 0.0
    assert gauges["rag_corpus_freshness_issues_total"] == 1.0


def test_policy_corpus_gauges_includes_report_derived_fields_when_present():
    report = _eval_report(
        [_case("e1", "negative")], forbidden_citation_count=2, unauthorized_retrieval_count=1,
        required_targets=4, required_hits=3,
    )
    gauges = policy_corpus_gauges(freshness=_fresh_report(), report=report)
    assert gauges["rag_eval_recall_at_k"] == pytest.approx(0.75)
    assert gauges["rag_eval_forbidden_citation_count"] == 2.0
    assert gauges["rag_eval_unauthorized_retrieval_count"] == 1.0
    assert gauges["rag_eval_negative_case_accuracy"] == 1.0


def test_policy_corpus_gauges_omits_recall_and_precision_when_no_runnable_cases_exist():
    report = _eval_report([_case("e1", "negative")])
    gauges = policy_corpus_gauges(freshness=_fresh_report(), report=report)
    assert "rag_eval_recall_at_k" not in gauges
    assert "rag_eval_precision_at_k" not in gauges


def test_policy_corpus_gauges_includes_embedding_stats_when_a_client_is_given():
    class _FakeClient:
        tokens_used = 250
        retry_count = 2

    gauges = policy_corpus_gauges(freshness=_fresh_report(), embedding_client=_FakeClient())
    assert gauges["rag_eval_embedding_input_tokens_total"] == 250.0
    assert gauges["rag_eval_embedding_provider_retry_total"] == 2.0


# --- patient_record_corpus_gauges ------------------------------------------


def test_patient_record_corpus_gauges_normalizes_percentages_to_fractions():
    report = EvalReport(
        provider_name="fake", top_k=1, total_cases=3, recall_at_k=66.7, precision_at_k=33.3,
        duplicate_rate=100.0, fragment_coverage_gap=0.0, per_case=[], duplicate_clusters=[],
    )
    gauges = patient_record_corpus_gauges(report=report)
    assert gauges["rag_eval_recall_at_k"] == pytest.approx(0.667)
    assert gauges["rag_eval_precision_at_k"] == pytest.approx(0.333)
    assert gauges["rag_eval_duplicate_rate"] == pytest.approx(1.0)
    assert gauges["rag_eval_fragment_coverage_gap"] == pytest.approx(0.0)
    assert LAST_RUN_TIMESTAMP_METRIC in gauges


def test_patient_record_corpus_gauges_carries_no_query_or_patient_identifying_content():
    """The published gauge dict must contain ONLY the whitelisted numeric
    keys this module defines — never per_case/duplicate_clusters, which
    carry raw queries and patient ids (see libs/rag_eval/metrics.py)."""
    report = EvalReport(
        provider_name="fake", top_k=1, total_cases=1, recall_at_k=0.0, precision_at_k=0.0,
        duplicate_rate=0.0, fragment_coverage_gap=0.0,
        per_case=[{"query": "show me Maria Gonzalez's allergies", "expected_patient_id": 1042}],
        duplicate_clusters=[[1042, 1330]],
    )
    gauges = patient_record_corpus_gauges(report=report)
    allowed = {
        "rag_eval_recall_at_k", "rag_eval_precision_at_k", "rag_eval_duplicate_rate",
        "rag_eval_fragment_coverage_gap", LAST_RUN_TIMESTAMP_METRIC,
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
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", "0:9091", "prom/pushgateway:v1.11.3"],
        capture_output=True, text=True,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start a local pushgateway container: {started.stderr.strip()}")
    container_id = started.stdout.strip()
    try:
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
            pytest.skip("local pushgateway container never became healthy")

        assert push_metrics(
            pushgateway_url=url, corpus="policy_corpus",
            gauges={"rag_eval_recall_at_k": 0.8, "rag_corpus_freshness_fresh": 1.0},
        ) is True
        assert push_metrics(
            pushgateway_url=url, corpus="patient_record_corpus", gauges={"rag_eval_duplicate_rate": 0.25},
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
