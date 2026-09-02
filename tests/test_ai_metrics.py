"""The AI metric contract: what is counted, and what may never become a label.

Values are read as BEFORE/AFTER DELTAS, never absolutes — the Prometheus
default registry is process-global and shared across every test file in this
run, exactly as tests/test_http_metrics.py and tests/test_roi_metrics.py
already do.

The privacy assertions here are the point of the file. A Prometheus label is
a permanent, scrapeable, unbounded-cardinality dimension; a patient id or a
raw provider error in one would be both a cardinality failure and a PHI leak,
and it would be invisible in ordinary use.
"""
import pytest

from libs.metrics import ai as ai_metrics

# Label NAMES that may never appear on any AI metric. Identifier- and
# content-shaped dimensions, per the telemetry boundary.
FORBIDDEN_LABEL_NAMES = {
    "patient_id", "patient", "user_id", "user", "actor", "actor_id", "reviewed_by",
    "correlation_id", "request_id", "provider_request_id", "draft_id", "visit_id",
    "insurance_id", "question", "answer", "prompt", "response", "text", "quote",
    "citation_id", "citation", "document", "error", "error_type", "exception",
    "message", "path", "url", "token", "credential",
}

AI_METRICS = (
    ai_metrics.BEDROCK_PROVIDER_CALLS,
    ai_metrics.BEDROCK_INPUT_TOKENS,
    ai_metrics.BEDROCK_OUTPUT_TOKENS,
    ai_metrics.BEDROCK_CALL_DURATION,
    ai_metrics.AGENT_RUNS,
    ai_metrics.AGENT_CITATIONS_PER_ANSWER,
    ai_metrics.AGENT_REVIEW_OUTCOMES,
    ai_metrics.AGENT_REVIEW_DURATION,
    ai_metrics.ELIGIBILITY_CIRCUIT_OPEN,
    ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS,
)


def _counter_value(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def _histogram_count(metric, **labels) -> float:
    child = metric.labels(**labels) if labels else metric
    for sample in child._child_samples():
        if sample[0] == "_count":
            return sample[2]
    return 0.0


def _gauge_value(metric) -> float:
    return metric._value.get()


# --- label discipline ------------------------------------------------------


def test_no_ai_metric_declares_an_identifier_or_content_label():
    """The invariant that has to hold for every metric in the module, not
    just the ones a test happens to exercise."""
    for metric in AI_METRICS:
        names = set(getattr(metric, "_labelnames", ()) or ())
        leaked = names & FORBIDDEN_LABEL_NAMES
        assert not leaked, f"{metric._name} declares forbidden label(s): {sorted(leaked)}"


def test_an_unrecognised_label_value_is_folded_rather_than_creating_a_series():
    """A caller passing something outside the vocabulary must not be able to
    grow the series count — an unbounded label is the cardinality failure
    this module exists to prevent."""
    before = _counter_value(
        ai_metrics.BEDROCK_PROVIDER_CALLS, provider="bedrock", model="m-1",
        use_case=ai_metrics.UNKNOWN, operation="converse", outcome=ai_metrics.UNKNOWN,
    )
    ai_metrics.record_provider_call(
        use_case="patient-1738-asked-about-their-a1c",  # not a known use case
        model_id="m-1", outcome="exploded: AccessDeniedException for arn:aws:...",
    )
    after = _counter_value(
        ai_metrics.BEDROCK_PROVIDER_CALLS, provider="bedrock", model="m-1",
        use_case=ai_metrics.UNKNOWN, operation="converse", outcome=ai_metrics.UNKNOWN,
    )
    assert after == before + 1, "both out-of-vocabulary values fold to the sentinel"


def test_an_absent_model_id_becomes_a_sentinel_not_an_empty_label():
    ai_metrics.record_provider_call(
        use_case="summary_agent_chat", model_id=None, outcome="provider_error",
    )
    assert _counter_value(
        ai_metrics.BEDROCK_PROVIDER_CALLS, provider="bedrock", model=ai_metrics.UNCONFIGURED,
        use_case="summary_agent_chat", operation="converse", outcome="provider_error",
    ) >= 1


def test_a_malformed_model_id_is_length_capped():
    ai_metrics.record_provider_call(
        use_case="summary_agent_chat", model_id="x" * 5000, outcome="success",
    )
    labels = {
        s.labels["model"]
        for m in ai_metrics.BEDROCK_PROVIDER_CALLS.collect() for s in m.samples
        if s.labels.get("model", "").startswith("xxxx")
    }
    assert labels, "the capped label should exist"
    assert all(len(v) <= 120 for v in labels)


# --- provider calls --------------------------------------------------------


def test_a_successful_provider_call_records_outcome_duration_and_tokens():
    labels = dict(provider="bedrock", model="model-a", use_case="summary_agent_chat")
    before_calls = _counter_value(ai_metrics.BEDROCK_PROVIDER_CALLS, **labels,
                                  operation="converse", outcome="success")
    before_in = _counter_value(ai_metrics.BEDROCK_INPUT_TOKENS, **labels)
    before_out = _counter_value(ai_metrics.BEDROCK_OUTPUT_TOKENS, **labels)
    before_dur = _histogram_count(ai_metrics.BEDROCK_CALL_DURATION, **labels, operation="converse")

    ai_metrics.record_provider_call(
        use_case="summary_agent_chat", model_id="model-a", outcome="success",
        duration_seconds=1.5, input_tokens=100, output_tokens=20,
    )

    assert _counter_value(ai_metrics.BEDROCK_PROVIDER_CALLS, **labels,
                          operation="converse", outcome="success") == before_calls + 1
    assert _counter_value(ai_metrics.BEDROCK_INPUT_TOKENS, **labels) == before_in + 100
    assert _counter_value(ai_metrics.BEDROCK_OUTPUT_TOKENS, **labels) == before_out + 20
    assert _histogram_count(ai_metrics.BEDROCK_CALL_DURATION, **labels,
                            operation="converse") == before_dur + 1


def test_a_failed_provider_call_is_recorded_and_carries_no_error_detail():
    """The failure path must be measured — an outage that only shows up as
    missing success counts is an outage nobody alerts on."""
    labels = dict(provider="bedrock", model="model-a", use_case="policy_navigator_chat")
    before = _counter_value(ai_metrics.BEDROCK_PROVIDER_CALLS, **labels,
                            operation="converse", outcome="provider_error")

    ai_metrics.record_provider_call(
        use_case="policy_navigator_chat", model_id="model-a", outcome="provider_error",
        duration_seconds=0.4,
    )

    assert _counter_value(ai_metrics.BEDROCK_PROVIDER_CALLS, **labels,
                          operation="converse", outcome="provider_error") == before + 1
    rendered = {s.labels.get("outcome") for m in ai_metrics.BEDROCK_PROVIDER_CALLS.collect()
                for s in m.samples}
    assert rendered <= ai_metrics.CALL_OUTCOMES | {ai_metrics.UNKNOWN}


def test_a_provider_that_reports_no_usage_contributes_no_token_counts():
    """The eligibility port discards Bedrock's usage block. Absent must stay
    absent — a zero would be a measurement never made."""
    labels = dict(provider="bedrock", model="model-elig", use_case="eligibility_agent_chat")
    before_in = _counter_value(ai_metrics.BEDROCK_INPUT_TOKENS, **labels)
    before_out = _counter_value(ai_metrics.BEDROCK_OUTPUT_TOKENS, **labels)

    ai_metrics.record_provider_call(
        use_case="eligibility_agent_chat", model_id="model-elig", operation="converse_stream",
        outcome="success", duration_seconds=2.0,
    )

    assert _counter_value(ai_metrics.BEDROCK_INPUT_TOKENS, **labels) == before_in
    assert _counter_value(ai_metrics.BEDROCK_OUTPUT_TOKENS, **labels) == before_out
    assert _counter_value(ai_metrics.BEDROCK_PROVIDER_CALLS, **labels,
                          operation="converse_stream", outcome="success") >= 1


# --- agent runs, citations, review ----------------------------------------


@pytest.mark.parametrize("reason", ["answered", "max_turns", "provider_error",
                                    "citation_invalid", "no_evidence"])
def test_every_termination_reason_is_a_recordable_bounded_value(reason):
    before = _counter_value(ai_metrics.AGENT_RUNS, use_case="policy_navigator_chat",
                            provenance_label="fallback", termination_reason=reason)
    ai_metrics.record_agent_run(use_case="policy_navigator_chat",
                                provenance_label="fallback", termination_reason=reason)
    assert _counter_value(ai_metrics.AGENT_RUNS, use_case="policy_navigator_chat",
                          provenance_label="fallback", termination_reason=reason) == before + 1


def test_a_surface_without_provenance_records_not_applicable():
    """The eligibility assistant has no real/fixture/fallback distinction.
    Borrowing one would report a provenance the code never determines."""
    before = _counter_value(ai_metrics.AGENT_RUNS, use_case="eligibility_agent_chat",
                            provenance_label=ai_metrics.NOT_APPLICABLE,
                            termination_reason="answered")
    ai_metrics.record_agent_run(use_case="eligibility_agent_chat",
                                provenance_label=ai_metrics.NOT_APPLICABLE,
                                termination_reason="answered")
    assert _counter_value(ai_metrics.AGENT_RUNS, use_case="eligibility_agent_chat",
                          provenance_label=ai_metrics.NOT_APPLICABLE,
                          termination_reason="answered") == before + 1


def test_a_provenance_label_enum_is_accepted_as_well_as_its_string():
    from libs.agent_provenance import ProvenanceLabel

    before = _counter_value(ai_metrics.AGENT_RUNS, use_case="summary_agent_chat",
                            provenance_label="real", termination_reason="answered")
    ai_metrics.record_agent_run(use_case="summary_agent_chat",
                                provenance_label=ProvenanceLabel.REAL,
                                termination_reason="answered")
    assert _counter_value(ai_metrics.AGENT_RUNS, use_case="summary_agent_chat",
                          provenance_label="real", termination_reason="answered") == before + 1


def test_citations_are_observed_per_completed_answer():
    before = _histogram_count(ai_metrics.AGENT_CITATIONS_PER_ANSWER, use_case="summary_agent_chat")
    ai_metrics.record_citations(use_case="summary_agent_chat", count=3)
    ai_metrics.record_citations(use_case="summary_agent_chat", count=0)
    assert _histogram_count(ai_metrics.AGENT_CITATIONS_PER_ANSWER,
                            use_case="summary_agent_chat") == before + 2


def test_a_review_records_its_outcome_and_waiting_time():
    before_outcome = _counter_value(ai_metrics.AGENT_REVIEW_OUTCOMES, outcome="approved")
    before_duration = _histogram_count(ai_metrics.AGENT_REVIEW_DURATION)

    ai_metrics.record_review(outcome="approved", duration_seconds=120.0)

    assert _counter_value(ai_metrics.AGENT_REVIEW_OUTCOMES, outcome="approved") == before_outcome + 1
    assert _histogram_count(ai_metrics.AGENT_REVIEW_DURATION) == before_duration + 1


def test_a_review_with_no_derivable_duration_still_counts_the_outcome():
    before_outcome = _counter_value(ai_metrics.AGENT_REVIEW_OUTCOMES, outcome="rejected")
    before_duration = _histogram_count(ai_metrics.AGENT_REVIEW_DURATION)

    ai_metrics.record_review(outcome="rejected", duration_seconds=None)

    assert _counter_value(ai_metrics.AGENT_REVIEW_OUTCOMES, outcome="rejected") == before_outcome + 1
    assert _histogram_count(ai_metrics.AGENT_REVIEW_DURATION) == before_duration, (
        "an underivable duration is not observed as zero"
    )


def test_no_pending_review_backlog_gauge_is_exposed():
    """Deliberate absence, asserted so it is not added casually: a
    process-local gauge would reset on restart while the database still held
    pending drafts, and a truthful one would mean a query per scrape."""
    assert not hasattr(ai_metrics, "AGENT_REVIEW_BACKLOG")


# --- circuit breaker -------------------------------------------------------


def test_a_circuit_transition_moves_the_counter_and_the_gauge():
    before = _counter_value(ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS,
                            from_state="closed", to_state="open")

    ai_metrics.record_circuit_transition("closed", "open")

    assert _counter_value(ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS,
                          from_state="closed", to_state="open") == before + 1
    assert _gauge_value(ai_metrics.ELIGIBILITY_CIRCUIT_OPEN) == 1

    ai_metrics.record_circuit_transition("open", "half_open")
    assert _gauge_value(ai_metrics.ELIGIBILITY_CIRCUIT_OPEN) == 0


def test_a_non_transition_is_not_counted():
    """A run of successes leaves the breaker CLOSED. Counting each one as a
    transition would make the graph unreadable and the alert meaningless."""
    before = _counter_value(ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS,
                            from_state="closed", to_state="closed")
    ai_metrics.record_circuit_transition("closed", "closed")
    assert _counter_value(ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS,
                          from_state="closed", to_state="closed") == before


# --- instrumentation must never break the caller --------------------------


@pytest.mark.parametrize("call", [
    lambda: ai_metrics.record_provider_call(use_case=None, outcome=None, duration_seconds="nope"),
    lambda: ai_metrics.record_agent_run(use_case=object(), provenance_label=object(),
                                        termination_reason=object()),
    lambda: ai_metrics.record_citations(use_case="summary_agent_chat", count="three"),
    lambda: ai_metrics.record_review(outcome=None, duration_seconds=object()),
    lambda: ai_metrics.record_circuit_transition(object(), object()),
])
def test_a_bad_recording_call_never_raises(call):
    call()  # would fail the request it is measuring, which instrumentation must not do
