"""The metrics are wired to the REAL agent paths, not just callable.

test_ai_metrics.py proves the recorders behave. This file proves the three
live surfaces actually reach them: a scripted model drives the genuine
LangChain loop / Converse port, and the counters move as a result. Without
this, every recorder could be correct and the dashboard still empty.

Deltas only — the Prometheus default registry is process-global.
"""
import json

import pytest

from conftest import load_module

from libs.agent_provenance import ProvenanceLabel, TraceRecorder
from libs.metrics import ai as ai_metrics
from libs.policy_corpus import RetrievalScope, RetrievedChunk

SCOPE = RetrievalScope(audiences=("patient",), workflows=("patient_summary",))
POL = "LAB-REL-001@1.2#overview"
POL_TEXT = "Results are shown exactly as the laboratory reported them."


def _calls(**labels) -> float:
    return ai_metrics.BEDROCK_PROVIDER_CALLS.labels(**labels)._value.get()


def _runs(**labels) -> float:
    return ai_metrics.AGENT_RUNS.labels(**labels)._value.get()


def _hist_count(metric, **labels) -> float:
    child = metric.labels(**labels) if labels else metric
    for sample in child._child_samples():
        if sample[0] == "_count":
            return sample[2]
    return 0.0


def _policy_tool_call(query="policy question", call_id="call_1"):
    """The navigator must actually retrieve before it cites — otherwise the
    citation validator correctly rejects the answer as citation_invalid."""
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{
        "name": "retrieve_policy", "args": {"query": query}, "id": call_id,
    }])


def _chunk(citation_id, text):
    source_id, rest = citation_id.split("@")
    version, section_id = rest.split("#")
    return RetrievedChunk(
        citation_id=citation_id, source_id=source_id, source_version=version, title="Policy",
        effective_date="2026-08-01", section_id=section_id, heading_path=("Policy",),
        score=0.9, text=text,
    )


class _FakeRetriever:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def retrieve(self, query, scope, limit):
        self.calls.append((query, scope, limit))
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx] if self._responses else []


def _scripted(responses, raises=None):
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _Scripted(BaseChatModel):
        model_id: str = "scripted-model-v0"
        script: list = []
        raise_with: object = None
        calls: int = 0

        @property
        def _llm_type(self):
            return "scripted"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.raise_with is not None:
                raise self.raise_with
            object.__setattr__(self, "calls", self.calls + 1)
            return ChatResult(generations=[ChatGeneration(
                message=self.script[min(self.calls - 1, len(self.script) - 1)])])

    return _Scripted(script=list(responses), raise_with=raises)


# --- summary agent ---------------------------------------------------------


def test_a_summary_agent_run_records_a_provider_call_and_a_run():
    from langchain_core.messages import AIMessage

    from libs.summary_agent.runtime import run_summary_agent

    call_labels = dict(provider="bedrock", model="scripted-model-v0",
                       use_case="summary_agent_chat", operation="converse", outcome="success")
    run_labels = dict(use_case="summary_agent_chat", provenance_label="fixture",
                      termination_reason="answered")
    before_calls = _calls(**call_labels)
    before_runs = _runs(**run_labels)
    before_citations = _hist_count(ai_metrics.AGENT_CITATIONS_PER_ANSWER,
                                   use_case="summary_agent_chat")

    final = AIMessage(content=json.dumps({
        "summary": f'"{POL_TEXT}"',
        "claims": [{"kind": "quote", "citation_id": POL, "quote": POL_TEXT}],
    }))
    result = run_summary_agent(
        scope=SCOPE, retriever=_FakeRetriever([[_chunk(POL, POL_TEXT)]]), actor_role="clinician",
        trace=TraceRecorder("corr-metrics-1"), model=_scripted([final]),
        label=ProvenanceLabel.FIXTURE,
    )

    assert result.termination_reason == "answered"
    assert _calls(**call_labels) == before_calls + 1
    assert _runs(**run_labels) == before_runs + 1
    assert _hist_count(ai_metrics.AGENT_CITATIONS_PER_ANSWER,
                       use_case="summary_agent_chat") == before_citations + 1


def test_a_summary_agent_provider_failure_records_the_failed_call_and_a_fallback_run():
    from libs.summary_agent.runtime import run_summary_agent

    run_labels = dict(use_case="summary_agent_chat", provenance_label="fallback",
                      termination_reason="provider_error")
    before_runs = _runs(**run_labels)

    result = run_summary_agent(
        scope=SCOPE, retriever=_FakeRetriever([[_chunk(POL, POL_TEXT)]]), actor_role="clinician",
        trace=TraceRecorder("corr-metrics-2"),
        model=_scripted([], raises=RuntimeError("bedrock is down")),
        label=ProvenanceLabel.FIXTURE,
    )

    assert result.termination_reason == "provider_error"
    assert _runs(**run_labels) == before_runs + 1
    assert _calls(provider="bedrock", model="scripted-model-v0", use_case="summary_agent_chat",
                  operation="converse", outcome="provider_error") >= 1


def test_a_fixture_run_contributes_no_token_counts():
    """The same REAL-only rule the durable usage rows follow: a scripted
    model's usage_metadata is not a provider measurement."""
    from langchain_core.messages import AIMessage

    from libs.summary_agent.runtime import run_summary_agent

    token_labels = dict(provider="bedrock", model="scripted-model-v0",
                        use_case="summary_agent_chat")
    before = ai_metrics.BEDROCK_INPUT_TOKENS.labels(**token_labels)._value.get()

    final = AIMessage(
        content=json.dumps({"summary": f'"{POL_TEXT}"',
                            "claims": [{"kind": "quote", "citation_id": POL, "quote": POL_TEXT}]}),
        usage_metadata={"input_tokens": 999, "output_tokens": 111, "total_tokens": 1110},
    )
    run_summary_agent(
        scope=SCOPE, retriever=_FakeRetriever([[_chunk(POL, POL_TEXT)]]), actor_role="clinician",
        trace=TraceRecorder("corr-metrics-3"), model=_scripted([final]),
        label=ProvenanceLabel.FIXTURE,
    )

    assert ai_metrics.BEDROCK_INPUT_TOKENS.labels(**token_labels)._value.get() == before


def test_a_real_labelled_run_does_record_tokens():
    from langchain_core.messages import AIMessage

    from libs.summary_agent.runtime import run_summary_agent

    token_labels = dict(provider="bedrock", model="scripted-model-v0",
                        use_case="summary_agent_chat")
    before_in = ai_metrics.BEDROCK_INPUT_TOKENS.labels(**token_labels)._value.get()
    before_out = ai_metrics.BEDROCK_OUTPUT_TOKENS.labels(**token_labels)._value.get()

    final = AIMessage(
        content=json.dumps({"summary": f'"{POL_TEXT}"',
                            "claims": [{"kind": "quote", "citation_id": POL, "quote": POL_TEXT}]}),
        usage_metadata={"input_tokens": 150, "output_tokens": 30, "total_tokens": 180},
    )
    run_summary_agent(
        scope=SCOPE, retriever=_FakeRetriever([[_chunk(POL, POL_TEXT)]]), actor_role="clinician",
        trace=TraceRecorder("corr-metrics-4"), model=_scripted([final]),
        label=ProvenanceLabel.REAL,
    )

    assert ai_metrics.BEDROCK_INPUT_TOKENS.labels(**token_labels)._value.get() == before_in + 150
    assert ai_metrics.BEDROCK_OUTPUT_TOKENS.labels(**token_labels)._value.get() == before_out + 30


# --- policy navigator ------------------------------------------------------


def test_a_policy_navigator_run_records_its_run_and_provider_call():
    from langchain_core.messages import AIMessage

    from libs.policy_navigator.runtime import run_policy_navigator

    nav_scope = RetrievalScope(audiences=("staff",), workflows=("policy_navigator",))
    citation = "SRC-001@1.0#intake"
    chunk = _chunk(citation, "Front desk verifies insurance at check-in.")

    run_labels = dict(use_case="policy_navigator_chat", provenance_label="fixture",
                      termination_reason="answered")
    before_runs = _runs(**run_labels)
    before_calls = _calls(provider="bedrock", model="scripted-model-v0",
                          use_case="policy_navigator_chat", operation="converse", outcome="success")

    answer = AIMessage(content=f"Front desk verifies insurance at check-in. [{citation}]")
    result = run_policy_navigator(
        "When is insurance verified?", scope=nav_scope,
        retriever=_FakeRetriever([[chunk]]), model=_scripted([_policy_tool_call(), answer]),
        label=ProvenanceLabel.FIXTURE,
    )

    assert result.termination_reason == "answered", result.termination_reason
    assert _runs(**run_labels) == before_runs + 1, "one RUN, however many turns it took"
    # TWO provider calls for one run: the retrieval turn and the answering
    # turn are separate round trips, and each is separately billable — which
    # is exactly why the call counter is per-call and the run counter is not.
    assert _calls(provider="bedrock", model="scripted-model-v0", use_case="policy_navigator_chat",
                  operation="converse", outcome="success") == before_calls + 2


def test_a_policy_navigator_refusal_records_the_run_but_no_citation_observation():
    """A refusal carries no citations by design; observing 0 would drag the
    histogram down for a reason unrelated to citation richness."""
    from langchain_core.messages import AIMessage

    from libs.policy_navigator.runtime import run_policy_navigator

    nav_scope = RetrievalScope(audiences=("staff",), workflows=("policy_navigator",))
    before_citations = _hist_count(ai_metrics.AGENT_CITATIONS_PER_ANSWER,
                                   use_case="policy_navigator_chat")

    answer = AIMessage(content="Policy says results are released immediately.")  # no citation
    result = run_policy_navigator(
        "Anything?", scope=nav_scope,
        retriever=_FakeRetriever([[_chunk("SRC-002@1.0#x", "Some approved text.")]]),
        model=_scripted([_policy_tool_call(), answer]), label=ProvenanceLabel.FIXTURE,
    )

    assert result.termination_reason == "no_evidence"
    assert _runs(use_case="policy_navigator_chat", provenance_label="fallback",
                 termination_reason="no_evidence") >= 1
    assert _hist_count(ai_metrics.AGENT_CITATIONS_PER_ANSWER,
                       use_case="policy_navigator_chat") == before_citations


# --- eligibility circuit breaker ------------------------------------------


def test_the_real_breaker_records_transitions_as_it_opens_and_closes():
    breaker_mod = load_module("services/eligibility-service/breaker.py", "breaker_metrics_mod")

    clock = {"t": 0.0}
    breaker = breaker_mod.CircuitBreaker(
        failure_threshold=2, reset_timeout_seconds=10.0, clock=lambda: clock["t"],
    )
    before_open = ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS.labels(
        from_state="closed", to_state="open")._value.get()
    before_half = ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS.labels(
        from_state="open", to_state="half_open")._value.get()

    breaker.record_failure()          # closed, below threshold
    breaker.record_failure()          # -> open
    assert breaker.state == breaker_mod.CircuitState.OPEN
    assert ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS.labels(
        from_state="closed", to_state="open")._value.get() == before_open + 1
    assert ai_metrics.ELIGIBILITY_CIRCUIT_OPEN._value.get() == 1

    clock["t"] = 20.0                 # timeout elapses -> half_open on read
    assert breaker.state == breaker_mod.CircuitState.HALF_OPEN
    assert ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS.labels(
        from_state="open", to_state="half_open")._value.get() == before_half + 1
    assert ai_metrics.ELIGIBILITY_CIRCUIT_OPEN._value.get() == 0

    breaker.record_success()          # -> closed
    assert breaker.state == breaker_mod.CircuitState.CLOSED


def test_repeated_successes_do_not_inflate_the_transition_counter():
    breaker_mod = load_module("services/eligibility-service/breaker.py", "breaker_metrics_mod2")
    breaker = breaker_mod.CircuitBreaker(failure_threshold=2)

    before = ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS.labels(
        from_state="closed", to_state="closed")._value.get()
    for _ in range(5):
        breaker.record_success()
    assert ai_metrics.ELIGIBILITY_CIRCUIT_TRANSITIONS.labels(
        from_state="closed", to_state="closed")._value.get() == before


# --- eligibility service exposes the series at all -------------------------


def test_eligibility_service_exposes_a_token_guarded_metrics_endpoint():
    """It owns the circuit-breaker and eligibility agent-run series, so
    without its own scrape target those series are unobservable."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app_mod = load_module("services/eligibility-service/app.py", "eligibility_metrics_app")
    app_mod.settings.internal_service_token = "a-token-long-enough-for-the-floor"
    client = TestClient(app_mod.app)

    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"X-Internal-Token": "a-token-long-enough-for-the-floor"})
    assert ok.status_code == 200
    assert "http_requests_total" in ok.text
