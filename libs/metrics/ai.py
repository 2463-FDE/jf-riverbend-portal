"""Real Prometheus metrics for the live online AI paths.

Declared at module scope for the same reason `libs/metrics/business.py` is:
`tests/conftest.py::load_module` re-execs a fresh copy of each service's
`app.py` per test file, and a second registration of the same metric name in
Prometheus's default registry raises `DuplicateTimeseriesError`. A normal
cached import executes once per process, so every fresh `app.py` copy shares
these exact objects.

WHAT IS MEASURED, AND WHERE. Each value is recorded at the point the outcome
becomes known — inside the agent middleware for a provider call, at the single
run-completion seam for an agent run, at the clinician decision for a review.
Nothing here queries PostgreSQL on a scrape; a Prometheus scrape only reads
counters already incremented in memory.

LABEL DISCIPLINE IS THE WHOLE POINT. Every label is a bounded enum or an
operator-configured identifier (provider, model, use case, operation, outcome,
provenance label, termination reason, circuit state). Never a patient, user,
draft or correlation id, never a provider request id, never a question,
prompt, response, retrieved document, citation text, or raw provider error.
An unrecognised value is folded to a sentinel rather than passed through, so a
caller cannot grow the series count by accident: an unbounded label is both a
Prometheus cardinality failure and, in this system, a plausible PHI leak.

Every recorder is best-effort and never raises — instrumentation must not fail
the request it is measuring, matching `libs/metrics/counters.py` and
`libs/tracing`.
"""
from typing import Optional

from prometheus_client import Counter, Gauge, Histogram

# --- bounded label vocabularies -------------------------------------------
# A value outside its vocabulary becomes UNKNOWN rather than a new series.
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
UNCONFIGURED = "unconfigured"

PROVIDERS = frozenset({"bedrock"})
USE_CASES = frozenset({
    "summary_agent_chat",
    "policy_navigator_chat",
    "eligibility_agent_chat",
})
OPERATIONS = frozenset({"converse", "converse_stream"})
# Per-CALL outcome. Deliberately coarse: a call either returned, raised, or
# (streaming only) was cancelled by its consumer closing the generator before
# it finished. How the RUN ended is a different question, answered by
# TERMINATION_REASONS.
CALL_OUTCOMES = frozenset({"success", "provider_error", "cancelled"})
TERMINATION_REASONS = frozenset({
    "answered", "max_turns", "provider_error", "citation_invalid", "no_evidence",
})
PROVENANCE_LABELS = frozenset({"real", "fixture", "fallback", NOT_APPLICABLE})
REVIEW_OUTCOMES = frozenset({"approved", "rejected"})
CIRCUIT_STATES = frozenset({"closed", "open", "half_open"})

# The model id is operator-configured (BEDROCK_MODEL_ID), so it is a bounded
# identifier in any one deployment rather than user input — but it still gets
# length-capped, because a malformed env value should not become a giant
# series name.
_MAX_MODEL_LABEL = 120


def _bounded(value, vocabulary: frozenset, default: str = UNKNOWN) -> str:
    """Fold `value` to a member of `vocabulary`, or to `default`."""
    if isinstance(value, str) and value in vocabulary:
        return value
    return default


def _model_label(model_id: Optional[str]) -> str:
    if not isinstance(model_id, str) or not model_id.strip():
        return UNCONFIGURED
    return model_id.strip()[:_MAX_MODEL_LABEL]


# --- provider-call metrics -------------------------------------------------
BEDROCK_PROVIDER_CALLS = Counter(
    "bedrock_provider_calls_total",
    "Model provider calls by provider, model, use case, operation and categorical outcome",
    ["provider", "model", "use_case", "operation", "outcome"],
)
BEDROCK_INPUT_TOKENS = Counter(
    "bedrock_input_tokens_total",
    "Input tokens reported by the provider (absent when the contract reports none)",
    ["provider", "model", "use_case"],
)
BEDROCK_OUTPUT_TOKENS = Counter(
    "bedrock_output_tokens_total",
    "Output tokens reported by the provider (absent when the contract reports none)",
    ["provider", "model", "use_case"],
)
BEDROCK_CALL_DURATION = Histogram(
    "bedrock_call_duration_seconds",
    "Wall-clock duration of one model provider call",
    ["provider", "model", "use_case", "operation"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)

# --- agent-run metrics -----------------------------------------------------
AGENT_RUNS = Counter(
    "agent_runs_total",
    "Completed agent runs by use case, provenance label and termination reason",
    ["use_case", "provenance_label", "termination_reason"],
)
AGENT_CITATIONS_PER_ANSWER = Histogram(
    "agent_citations_per_answer",
    "Citations carried by a completed answer or draft",
    ["use_case"],
    buckets=(0, 1, 2, 3, 5, 8, 13),
)

# --- clinician review metrics ---------------------------------------------
# NO pending-review backlog gauge is exposed. A process-local gauge would
# reset to zero on every restart while the database still held pending
# drafts, and a truthful one would mean querying PostgreSQL on each scrape —
# which this module exists to avoid. An untruthful gauge is worse than an
# absent one, so the backlog stays a database question.
AGENT_REVIEW_OUTCOMES = Counter(
    "agent_review_outcomes_total",
    "Clinician review decisions on agent drafts by outcome",
    ["outcome"],
)
AGENT_REVIEW_DURATION = Histogram(
    "agent_review_duration_seconds",
    "Elapsed time from draft generation to clinician decision",
    buckets=(30, 60, 300, 900, 3600, 14400, 86400),
)

# --- eligibility circuit breaker ------------------------------------------
ELIGIBILITY_CIRCUIT_OPEN = Gauge(
    "eligibility_circuit_open",
    "1 while the payer eligibility circuit breaker is open, else 0",
)
ELIGIBILITY_CIRCUIT_TRANSITIONS = Counter(
    "eligibility_circuit_transitions_total",
    "Payer eligibility circuit breaker state transitions",
    ["from_state", "to_state"],
)


def record_provider_call(
    *,
    use_case: str,
    provider: str = "bedrock",
    model_id: Optional[str] = None,
    operation: str = "converse",
    outcome: str,
    duration_seconds: Optional[float] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> None:
    """One provider round-trip's outcome, duration and reported token usage.

    `input_tokens`/`output_tokens` are left ABSENT when the provider contract
    reports none — the eligibility path's raw Converse port discards the
    provider's usage block, so that surface records calls, outcomes and
    duration and contributes nothing to the token counters. A zero would be a
    measurement this system never made.
    """
    try:
        provider_l = _bounded(provider, PROVIDERS)
        model_l = _model_label(model_id)
        use_case_l = _bounded(use_case, USE_CASES)
        operation_l = _bounded(operation, OPERATIONS, default="converse")
        outcome_l = _bounded(outcome, CALL_OUTCOMES)

        BEDROCK_PROVIDER_CALLS.labels(
            provider=provider_l, model=model_l, use_case=use_case_l,
            operation=operation_l, outcome=outcome_l,
        ).inc()

        if duration_seconds is not None and duration_seconds >= 0:
            BEDROCK_CALL_DURATION.labels(
                provider=provider_l, model=model_l, use_case=use_case_l, operation=operation_l,
            ).observe(float(duration_seconds))

        if isinstance(input_tokens, int) and input_tokens > 0:
            BEDROCK_INPUT_TOKENS.labels(
                provider=provider_l, model=model_l, use_case=use_case_l,
            ).inc(input_tokens)
        if isinstance(output_tokens, int) and output_tokens > 0:
            BEDROCK_OUTPUT_TOKENS.labels(
                provider=provider_l, model=model_l, use_case=use_case_l,
            ).inc(output_tokens)
    except Exception:  # instrumentation must never fail the caller
        pass


def record_agent_run(*, use_case: str, provenance_label, termination_reason: str) -> None:
    """One completed agent run, however it ended.

    `provenance_label` accepts a `ProvenanceLabel` or its string value; a
    surface with no real/fixture/fallback distinction of its own (the
    eligibility assistant) records `not_applicable` rather than borrowing a
    label it does not actually produce.
    """
    try:
        label = getattr(provenance_label, "value", provenance_label)
        AGENT_RUNS.labels(
            use_case=_bounded(use_case, USE_CASES),
            provenance_label=_bounded(label, PROVENANCE_LABELS),
            termination_reason=_bounded(termination_reason, TERMINATION_REASONS),
        ).inc()
    except Exception:
        pass


def record_citations(*, use_case: str, count: int) -> None:
    """How many citations a completed answer or draft carried."""
    try:
        if isinstance(count, int) and count >= 0:
            AGENT_CITATIONS_PER_ANSWER.labels(use_case=_bounded(use_case, USE_CASES)).observe(count)
    except Exception:
        pass


def record_review(*, outcome: str, duration_seconds: Optional[float] = None) -> None:
    """A clinician's approve/reject decision, and how long it waited."""
    try:
        AGENT_REVIEW_OUTCOMES.labels(outcome=_bounded(outcome, REVIEW_OUTCOMES)).inc()
        if duration_seconds is not None and duration_seconds >= 0:
            AGENT_REVIEW_DURATION.observe(float(duration_seconds))
    except Exception:
        pass


def record_circuit_transition(from_state, to_state) -> None:
    """A breaker state change, plus the open/closed gauge that follows from it."""
    try:
        previous = _bounded(getattr(from_state, "value", from_state), CIRCUIT_STATES)
        current = _bounded(getattr(to_state, "value", to_state), CIRCUIT_STATES)
        if previous == current:
            return  # not a transition; do not inflate the counter
        ELIGIBILITY_CIRCUIT_TRANSITIONS.labels(from_state=previous, to_state=current).inc()
        ELIGIBILITY_CIRCUIT_OPEN.set(1 if current == "open" else 0)
    except Exception:
        pass
