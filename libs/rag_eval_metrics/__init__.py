"""Sanitized batch RAG-evaluation metrics publishing — W10 Metrics Stage 5.

Publishes ONLY the aggregate, numeric result of an explicit OFFLINE
evaluation run (db/policy_corpus_evaluate.py for the policy corpus,
libs/rag_eval/harness.py for the patient-record corpus) to a local
Prometheus Pushgateway (observability profile). This module never runs an
evaluation itself and is never on any interactive request path — it is only
ever called after a caller already has a finished report in hand, and only
when explicitly asked to publish (RAG_EVAL_PUSHGATEWAY_URL unset/empty means
"do not publish": both callers treat that as a no-op, not an error).

Every gauge-building function below reads ONLY the aggregate scalar fields
of the caller's report objects — a query, answer, document, citation, or
case identifier is never a parameter these functions could accept, let
alone export as a label or value. `push_metrics()` never raises: a push
failure (network, gateway down, malformed value) is caught and reported as
`False`, so it can never change the evaluation's own exit code or corrupt
its printed evidence report.

Both corpora publish under the SAME metric names, distinguished by a
`corpus` label ("policy_corpus" | "patient_record_corpus") set via
Pushgateway's grouping key — so every rate/accuracy value here is
normalized to a 0-1 fraction. libs.rag_eval.metrics.EvalReport reports its
own recall/precision/duplicate-rate/fragment-coverage-gap as 0-100
percentages; this module divides them by 100 before publishing so the two
corpora's numbers are directly comparable on one Grafana panel rather than
silently disagreeing in scale under the same metric name.
"""
import logging
import time
from typing import Dict, Optional

log = logging.getLogger(__name__)

# One shared timestamp key across every push — Grafana panels and the
# staleness alert compute "evaluation age" as time() - this gauge, never a
# separately tracked value that could drift from what was actually published.
LAST_RUN_TIMESTAMP_METRIC = "rag_eval_last_run_timestamp_seconds"

_FRESHNESS_ISSUE_FIELDS = (
    "missing_documents", "extra_active_documents", "document_hash_mismatches",
    "missing_embeddings", "extra_active_embeddings", "chunk_hash_mismatches", "embedding_hash_mismatches",
)


def push_metrics(*, pushgateway_url: str, corpus: str, gauges: Dict[str, float]) -> bool:
    """Push a flat set of sanitized numeric gauges for one evaluation
    `corpus` to a local Pushgateway. Returns True on success, False on ANY
    failure — never raises. Every push REPLACES the previous snapshot for
    this corpus (Pushgateway's job+grouping-key semantics), which is exactly
    the "latest sanitized run" the stage asks for, not a running total —
    Gauges, not Counters, for every value here."""
    if not pushgateway_url:
        return False
    try:
        from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

        registry = CollectorRegistry()
        for name, value in gauges.items():
            Gauge(name, f"W10 Metrics Stage 5 batch RAG evaluation: {name}", registry=registry).set(float(value))
        push_to_gateway(
            pushgateway_url, job="rag_eval", grouping_key={"corpus": corpus}, registry=registry, timeout=5,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a push failure must never propagate
        log.warning("rag_eval metrics push failed (corpus=%s, error_type=%s)", corpus, type(exc).__name__)
        return False


def negative_case_accuracy(report) -> Optional[float]:
    """Fraction of NEGATIVE (unanswerable-by-design) evaluation cases whose
    retrieval avoided every forbidden/unauthorized hit. This is this
    harness's explicit definition of a "refusal-correct" negative case: it
    measures RETRIEVAL restraint, not the live agent's own refusal wording
    (EvaluationReport.as_dict() already marks the latter
    "not_evaluated_by_retrieval_harness" — unchanged here)."""
    negatives = [r for r in report.results if r.classification == "negative"]
    if not negatives:
        return None
    return sum(1 for r in negatives if r.retrieval_passed) / len(negatives)


def freshness_issue_count(freshness) -> int:
    """Total count of every kind of manifest/database disagreement a
    CorpusFreshnessReport can carry — a single bounded number for
    dashboards/alerts; the full identified lists remain in the script's own
    printed JSON evidence report for a human operator, never exported here."""
    return sum(len(getattr(freshness, field)) for field in _FRESHNESS_ISSUE_FIELDS)


def policy_corpus_gauges(*, freshness, report=None, embedding_client=None) -> Dict[str, float]:
    """The sanitized, numeric-only gauge set for one policy-corpus
    evaluation run. `freshness` is a libs.policy_corpus.freshness.
    CorpusFreshnessReport, always required. `report` (a libs.policy_corpus.
    evaluation.EvaluationReport) is optional: a stale corpus or a
    verify-only run has no evaluation to report yet, but its freshness
    numbers must still be publishable on their own so the staleness alert
    can fire without ever needing a completed evaluation. A metric whose
    value is None (e.g. no runnable cases existed at all) is omitted, never
    published as 0 or guessed."""
    values: Dict[str, Optional[float]] = {
        "rag_corpus_freshness_fresh": 1.0 if freshness.is_fresh else 0.0,
        "rag_corpus_expected_documents": float(freshness.expected_documents),
        "rag_corpus_database_documents": float(freshness.database_documents),
        "rag_corpus_expected_chunks": float(freshness.expected_chunks),
        "rag_corpus_embedded_chunks": float(freshness.embedded_chunks),
        "rag_corpus_freshness_issues_total": float(freshness_issue_count(freshness)),
        LAST_RUN_TIMESTAMP_METRIC: time.time(),
    }
    if report is not None:
        values["rag_eval_recall_at_k"] = report.recall_at_k
        values["rag_eval_precision_at_k"] = report.precision_at_k
        values["rag_eval_citation_target_accuracy"] = report.citation_target_accuracy
        values["rag_eval_negative_case_accuracy"] = negative_case_accuracy(report)
        values["rag_eval_forbidden_citation_count"] = float(report.forbidden_citation_count)
        values["rag_eval_unauthorized_retrieval_count"] = float(report.unauthorized_retrieval_count)
        values["rag_eval_case_coverage"] = report.case_coverage
    if embedding_client is not None:
        values["rag_eval_embedding_input_tokens_total"] = float(embedding_client.tokens_used)
        values["rag_eval_embedding_provider_retry_total"] = float(embedding_client.retry_count)
    return {name: value for name, value in values.items() if value is not None}


def patient_record_corpus_gauges(*, report, embedding_client=None) -> Dict[str, float]:
    """The sanitized, numeric-only gauge set for one patient-record-corpus
    (AUD-09 identity-fragmentation) evaluation run. `report` is a
    libs.rag_eval.metrics.EvalReport, whose four score fields are 0-100
    percentages — divided by 100 here (see module docstring)."""
    values: Dict[str, Optional[float]] = {
        "rag_eval_recall_at_k": report.recall_at_k / 100.0,
        "rag_eval_precision_at_k": report.precision_at_k / 100.0,
        "rag_eval_duplicate_rate": report.duplicate_rate / 100.0,
        "rag_eval_fragment_coverage_gap": report.fragment_coverage_gap / 100.0,
        LAST_RUN_TIMESTAMP_METRIC: time.time(),
    }
    if embedding_client is not None:
        values["rag_eval_embedding_input_tokens_total"] = float(embedding_client.tokens_used)
        values["rag_eval_embedding_provider_retry_total"] = float(embedding_client.retry_count)
    return values
