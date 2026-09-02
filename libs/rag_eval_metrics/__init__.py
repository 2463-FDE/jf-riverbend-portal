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

FRESHNESS vs EVALUATION are two independent snapshots, pushed under two
independent Pushgateway grouping identities (`kind="freshness"` /
`kind="evaluation"`, alongside `corpus`) and two independent timestamp
metrics. This is deliberate, not incidental: `push_to_gateway` REPLACES the
ENTIRE metric set under one exact grouping key on every push. Before this
split, a stale-corpus or --verify-only run — which has freshness numbers but
no completed evaluation — would push under the SAME grouping key a full
evaluation last used, silently ERASING that evaluation's recall/precision/
accuracy series (they're simply absent from a freshness-only push). Keeping
the two kinds under separate grouping keys means a freshness-only push can
never touch — and can never delete — the last completed evaluation's
metrics, and vice versa. A full, successful evaluation run publishes BOTH
snapshots (freshness first, since it was already computed to decide the run
should proceed, then evaluation).

Both corpora publish under the SAME metric names, distinguished by a
`corpus` label ("policy_corpus" | "patient_record_corpus") set via
Pushgateway's grouping key. libs.rag_eval.metrics.EvalReport reports its own
recall/precision/duplicate-rate/fragment-coverage-gap as 0-100 percentages;
this module divides them by 100 before publishing so both corpora's values
share one unit convention (0-1 fractions) on the same axis. Sharing a UNIT
is not the same as being COMPARABLE: each corpus keeps its own,
independently appropriate retrieval cutoff (`rag_eval_top_k` — the policy
corpus defaults to 5, the patient-record corpus to 1; see
db/policy_corpus_evaluate.py's --top-k and RAG_EVAL_TOP_K respectively), so
recall@5 for one corpus and recall@1 for the other are never a fair
side-by-side comparison. `rag_eval_top_k` is published specifically so a
dashboard or query can check this before treating two numbers as
comparable, rather than assuming it.
"""
import logging
import time
from typing import Dict, Optional, Sequence

log = logging.getLogger(__name__)

# Two INDEPENDENT timestamps — see module docstring. Never conflated: a
# freshness-only push updates ONLY the first; a completed evaluation
# updates ONLY the second (a full run's freshness half also updates the
# first, since it re-checked freshness to decide whether to proceed).
FRESHNESS_CHECKED_AT_METRIC = "rag_corpus_freshness_checked_at_seconds"
LAST_RUN_TIMESTAMP_METRIC = "rag_eval_last_run_timestamp_seconds"

MRR_METRIC = "rag_eval_mrr"

_FRESHNESS_ISSUE_FIELDS = (
    "missing_documents", "extra_active_documents", "document_hash_mismatches",
    "missing_embeddings", "extra_active_embeddings", "chunk_hash_mismatches", "embedding_hash_mismatches",
)


def push_metrics(*, pushgateway_url: str, corpus: str, kind: str, gauges: Dict[str, float]) -> bool:
    """Push a flat set of sanitized numeric gauges for one evaluation
    `corpus` and one snapshot `kind` ("freshness" | "evaluation") to a local
    Pushgateway. Returns True on success, False on ANY failure — never
    raises. Every push REPLACES the previous snapshot for this EXACT
    (corpus, kind) pair only — see module docstring for why `kind` exists
    and must never be dropped from the grouping key. Gauges, not Counters,
    for every value here: each push is "the latest run's numbers", not a
    running total."""
    if not pushgateway_url:
        return False
    try:
        from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

        registry = CollectorRegistry()
        for name, value in gauges.items():
            Gauge(name, f"W10 Metrics Stage 5 batch RAG evaluation: {name}", registry=registry).set(float(value))
        push_to_gateway(
            pushgateway_url, job="rag_eval", grouping_key={"corpus": corpus, "kind": kind},
            registry=registry, timeout=5,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — a push failure must never propagate
        log.warning("rag_eval metrics push failed (corpus=%s, kind=%s, error_type=%s)", corpus, kind, type(exc).__name__)
        return False


def negative_case_retrieval_safety_rate(report) -> Optional[float]:
    """Fraction of NEGATIVE (unanswerable-by-design) evaluation cases whose
    retrieval avoided every forbidden/unauthorized hit.

    This is NOT agent refusal correctness — it never runs the live agent and
    has no notion of what it would have said. It measures RETRIEVAL
    restraint only: whether the retriever itself stayed inside its
    forbidden/authorized bounds on a case that should have found nothing.
    EvaluationReport.as_dict() already marks true refusal-wording accuracy
    "not_evaluated_by_retrieval_harness"; this metric does not change that
    and is not a substitute for it. Building an agent-level refusal
    evaluator is out of scope for this change.
    """
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


def _reciprocal_rank(retrieved_identities: Sequence[str], required_targets: Sequence[str]) -> Optional[float]:
    """1/rank of the FIRST required target found in `retrieved_identities`
    (1-indexed, rank-ordered) — the standard reciprocal-rank definition,
    correctly handling MULTIPLE required targets by using whichever one is
    found earliest, never averaging or double-counting them. `None` when
    `required_targets` is empty (rank is undefined, not zero — a case with
    nothing to rank against is excluded from the mean, never scored as a
    miss). `0.0` (a genuine, countable miss) when targets exist but none
    appear anywhere in the retrieved list."""
    if not required_targets:
        return None
    targets = set(required_targets)
    for rank, identity in enumerate(retrieved_identities, start=1):
        if identity in targets:
            return 1.0 / rank
    return 0.0


def mean_reciprocal_rank(report) -> Optional[float]:
    """MRR over every RUNNABLE policy-corpus case. `None` — never a guessed
    0.0 — when there are no runnable cases to rank at all (nothing to
    average), which is a different, worse condition than "ranked and always
    missed" (which correctly averages to 0.0)."""
    scores = [
        rank for rank in (
            _reciprocal_rank(r.retrieved_identities, r.required_targets)
            for r in report.results if r.classification == "runnable"
        )
        if rank is not None
    ]
    if not scores:
        return None
    return sum(scores) / len(scores)


def mean_reciprocal_rank_patient_corpus(report) -> Optional[float]:
    """MRR over every patient-record-corpus gold case, from `report.per_case`
    (each entry already carries `expected_record_ids` and the rank-ordered
    `retrieved_record_ids` — see libs/rag_eval/metrics.py). `None` when
    there are no cases at all."""
    scores = [
        rank for rank in (
            _reciprocal_rank(case["retrieved_record_ids"], case["expected_record_ids"])
            for case in report.per_case
        )
        if rank is not None
    ]
    if not scores:
        return None
    return sum(scores) / len(scores)


def policy_corpus_freshness_gauges(*, freshness) -> Dict[str, float]:
    """The sanitized, numeric-only gauge set for a policy-corpus FRESHNESS
    check — published every run (stale, --verify-only, or as half of a full
    evaluation), independent of whether an evaluation ever completes. See
    module docstring for why this is a separate snapshot/timestamp from
    policy_corpus_evaluation_gauges."""
    return {
        "rag_corpus_freshness_fresh": 1.0 if freshness.is_fresh else 0.0,
        "rag_corpus_expected_documents": float(freshness.expected_documents),
        "rag_corpus_database_documents": float(freshness.database_documents),
        "rag_corpus_expected_chunks": float(freshness.expected_chunks),
        "rag_corpus_embedded_chunks": float(freshness.embedded_chunks),
        "rag_corpus_freshness_issues_total": float(freshness_issue_count(freshness)),
        FRESHNESS_CHECKED_AT_METRIC: time.time(),
    }


def policy_corpus_evaluation_gauges(*, report, embedding_client=None) -> Dict[str, float]:
    """The sanitized, numeric-only gauge set for one COMPLETED policy-corpus
    evaluation run. `report` (a libs.policy_corpus.evaluation.
    EvaluationReport) is required — this is only ever called after an
    evaluation genuinely finished. A metric whose value is None (e.g. no
    runnable cases existed at all) is omitted, never published as 0 or
    guessed."""
    values: Dict[str, Optional[float]] = {
        "rag_eval_top_k": float(report.top_k),
        "rag_eval_recall_at_k": report.recall_at_k,
        "rag_eval_precision_at_k": report.precision_at_k,
        MRR_METRIC: mean_reciprocal_rank(report),
        "rag_eval_citation_target_accuracy": report.citation_target_accuracy,
        "rag_eval_negative_case_retrieval_safety_rate": negative_case_retrieval_safety_rate(report),
        "rag_eval_forbidden_citation_count": float(report.forbidden_citation_count),
        "rag_eval_unauthorized_retrieval_count": float(report.unauthorized_retrieval_count),
        "rag_eval_case_coverage": report.case_coverage,
        LAST_RUN_TIMESTAMP_METRIC: time.time(),
    }
    if embedding_client is not None:
        values["rag_eval_embedding_input_tokens_total"] = float(embedding_client.tokens_used)
        values["rag_eval_embedding_provider_retry_total"] = float(embedding_client.retry_count)
    return {name: value for name, value in values.items() if value is not None}


def patient_record_corpus_gauges(*, report, embedding_client=None) -> Dict[str, float]:
    """The sanitized, numeric-only gauge set for one patient-record-corpus
    (AUD-09 identity-fragmentation) evaluation run. `report` is a
    libs.rag_eval.metrics.EvalReport, whose four score fields are 0-100
    percentages — divided by 100 here (see module docstring). This corpus
    has no freshness concept of its own (no manifest/database parity check
    exists for it), so it only ever publishes under kind="evaluation"."""
    values: Dict[str, Optional[float]] = {
        "rag_eval_top_k": float(report.top_k),
        "rag_eval_recall_at_k": report.recall_at_k / 100.0,
        "rag_eval_precision_at_k": report.precision_at_k / 100.0,
        MRR_METRIC: mean_reciprocal_rank_patient_corpus(report),
        "rag_eval_duplicate_rate": report.duplicate_rate / 100.0,
        "rag_eval_fragment_coverage_gap": report.fragment_coverage_gap / 100.0,
        LAST_RUN_TIMESTAMP_METRIC: time.time(),
    }
    if embedding_client is not None:
        values["rag_eval_embedding_input_tokens_total"] = float(embedding_client.tokens_used)
        values["rag_eval_embedding_provider_retry_total"] = float(embedding_client.retry_count)
    return {name: value for name, value in values.items() if value is not None}
