"""Unit tests for eligibility-service's agent wiring (agent_wiring.py):
memoized safe construction of the Stage 2 AgentRuntime, safe degrade when
unavailable, and visit-context binding used by the Stage 3 chat endpoint.

No real boto3/Bedrock call is ever made: with the default BEDROCK_MODEL_ID
(unset, or the repo's own "changeme" placeholder), construction fails inside
BedrockConverseToolModel.__init__ BEFORE `import boto3` runs — the same
ProviderNotConfiguredError path libs/llm_client's bedrock_provider already
proves in tests/test_bedrock_provider.py.
"""
from datetime import datetime, timezone

import pytest

from conftest import load_module
from libs.eligibility_agent.contracts import EligibilityStatus, TerminationReason, VisitContext, VisitTurnResult

agent_wiring = load_module("services/eligibility-service/agent_wiring.py", "eligibility_agent_wiring")


@pytest.fixture(autouse=True)
def _reset_module_singletons():
    agent_wiring._runtime = None
    agent_wiring._runtime_build_failed = False
    agent_wiring._memory = None
    agent_wiring._redis_client = None
    yield
    agent_wiring._runtime = None
    agent_wiring._runtime_build_failed = False
    agent_wiring._memory = None
    agent_wiring._redis_client = None


class _FakeVisitMemory:
    def __init__(self):
        self.store = {}
        self.put_calls = 0

    def get(self, visit_id):
        return self.store.get(visit_id)

    def put(self, context):
        self.put_calls += 1
        self.store[context.visit_id] = context


class _FakeRuntime:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def handle_message(self, visit_id, message):
        self.calls.append((visit_id, message))
        return self._result


class _FakeStreamingRuntime(_FakeRuntime):
    def __init__(self, result, events):
        super().__init__(result)
        self._events = events
        self.stream_calls = []

    def handle_message_stream(self, visit_id, message):
        self.stream_calls.append((visit_id, message))
        yield from self._events


# --- get_agent_runtime: unconfigured provider degrades, and only once --------


def test_unconfigured_provider_returns_none(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("ELIGIBILITY_AGENT_RUNTIME", raising=False)

    assert agent_wiring.get_agent_runtime() is None


def test_unconfigured_provider_failure_is_cached_not_retried(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    calls = {"n": 0}
    real_build = agent_wiring.build_agent_runtime

    def counting_build(*a, **k):
        calls["n"] += 1
        return real_build(*a, **k)

    monkeypatch.setattr(agent_wiring, "build_agent_runtime", counting_build)

    assert agent_wiring.get_agent_runtime() is None
    assert agent_wiring.get_agent_runtime() is None
    assert calls["n"] == 1  # second call used the cached failure, not a retry


def test_successfully_built_runtime_is_memoized(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(agent_wiring, "build_agent_runtime", lambda **k: sentinel)

    first = agent_wiring.get_agent_runtime()
    second = agent_wiring.get_agent_runtime()

    assert first is sentinel
    assert second is sentinel


# --- Stage 2 (feature-readiness): ollama runtime, through the real wiring --
#
# Unlike the tests above, this exercises the REAL build_agent_runtime (not
# mocked) end-to-end through get_agent_runtime() — proves the wiring layer
# actually produces a working local-demo runtime, not just that it degrades
# safely when unconfigured.


def test_ollama_runtime_builds_successfully_through_the_real_wiring(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")

    runtime = agent_wiring.get_agent_runtime()

    assert runtime is not None
    from libs.eligibility_agent.ollama_tool_port import OllamaToolCapableModel
    from libs.eligibility_agent.runtimes.raw_bedrock import RawBedrockAgentRuntime

    assert isinstance(runtime, RawBedrockAgentRuntime)
    assert isinstance(runtime._model, OllamaToolCapableModel)


def test_unconfigured_ollama_runtime_degrades_to_none_through_the_real_wiring(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "ollama")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    assert agent_wiring.get_agent_runtime() is None


# --- handle_visit_message: safe degrade vs delegation ------------------------


def test_handle_visit_message_degrades_safely_when_runtime_unavailable(monkeypatch):
    monkeypatch.setattr(agent_wiring, "get_agent_runtime", lambda: None)

    result = agent_wiring.handle_visit_message("visit-1", "am I covered?")

    assert result.termination_reason == TerminationReason.PROVIDER_ERROR
    assert result.tool_called is False
    assert result.turns_used == 0
    assert result.eligibility_status is None
    assert "manually" in result.reply.lower()


def test_handle_visit_message_delegates_to_the_real_runtime(monkeypatch):
    expected = VisitTurnResult(
        visit_id="visit-1",
        reply="You're covered.",
        tool_called=True,
        termination_reason=TerminationReason.ANSWERED,
        turns_used=2,
    )
    fake_runtime = _FakeRuntime(expected)
    monkeypatch.setattr(agent_wiring, "get_agent_runtime", lambda: fake_runtime)

    result = agent_wiring.handle_visit_message("visit-1", "am I covered?")

    assert result is expected
    assert fake_runtime.calls == [("visit-1", "am I covered?")]


# --- stream_visit_message (w-9-2-planner P1b) --------------------------------


def test_stream_visit_message_degrades_safely_when_runtime_unavailable(monkeypatch):
    monkeypatch.setattr(agent_wiring, "get_agent_runtime", lambda: None)

    events = list(agent_wiring.stream_visit_message("visit-1", "am I covered?"))

    assert len(events) == 1
    assert events[0].kind == "error"
    assert "manually" in events[0].text.lower()
    assert events[0].termination_reason == TerminationReason.PROVIDER_ERROR


def test_stream_visit_message_delegates_to_a_streaming_capable_runtime(monkeypatch):
    from libs.eligibility_agent.contracts import VisitStreamEvent

    scripted = [
        VisitStreamEvent(kind="delta", text="You're "),
        VisitStreamEvent(kind="delta", text="covered."),
        VisitStreamEvent(kind="done", tool_called=True, termination_reason=TerminationReason.ANSWERED, turns_used=2),
    ]
    fake_runtime = _FakeStreamingRuntime(result=None, events=scripted)
    monkeypatch.setattr(agent_wiring, "get_agent_runtime", lambda: fake_runtime)

    events = list(agent_wiring.stream_visit_message("visit-1", "am I covered?"))

    assert events == scripted
    assert fake_runtime.stream_calls == [("visit-1", "am I covered?")]


def test_stream_visit_message_falls_back_to_one_shot_replay_for_a_non_streaming_runtime(monkeypatch):
    # A runtime without handle_message_stream (langchain — never wired into
    # a running service) must still produce a usable, correctly-shaped
    # stream: the complete reply as one delta, then one done event.
    expected = VisitTurnResult(
        visit_id="visit-1",
        reply="You're covered.",
        tool_called=True,
        eligibility_status=EligibilityStatus.ACTIVE,
        termination_reason=TerminationReason.ANSWERED,
        turns_used=2,
    )
    fake_runtime = _FakeRuntime(expected)  # no handle_message_stream attribute at all
    monkeypatch.setattr(agent_wiring, "get_agent_runtime", lambda: fake_runtime)

    events = list(agent_wiring.stream_visit_message("visit-1", "am I covered?"))

    assert [e.kind for e in events] == ["delta", "done"]
    assert events[0].text == "You're covered."
    assert events[1].eligibility_status == EligibilityStatus.ACTIVE
    assert events[1].turns_used == 2


def test_stream_visit_message_fallback_provider_error_is_one_error_event_not_delta_plus_error(monkeypatch):
    # Regression guard: the safe reply text must not be sent twice (once as
    # a delta, once as the error event's text).
    expected = VisitTurnResult(
        visit_id="visit-1",
        reply=agent_wiring.UNAVAILABLE_REPLY,
        tool_called=False,
        termination_reason=TerminationReason.PROVIDER_ERROR,
        turns_used=0,
    )
    fake_runtime = _FakeRuntime(expected)
    monkeypatch.setattr(agent_wiring, "get_agent_runtime", lambda: fake_runtime)

    events = list(agent_wiring.stream_visit_message("visit-1", "check now"))

    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].text == agent_wiring.UNAVAILABLE_REPLY


# --- bind_visit_context -------------------------------------------------------


def test_bind_visit_context_creates_a_new_context_when_none_exists(monkeypatch):
    fake_memory = _FakeVisitMemory()
    monkeypatch.setattr(agent_wiring, "get_visit_memory", lambda: fake_memory)

    agent_wiring.bind_visit_context("visit-1", patient_id=42, insurance_id="MEM1")

    stored = fake_memory.get("visit-1")
    assert stored.patient_id == 42
    assert stored.insurance_id == "MEM1"


def test_bind_visit_context_merges_into_an_existing_context_without_clobbering_other_fields(monkeypatch):
    fake_memory = _FakeVisitMemory()
    fake_memory.put(
        VisitContext(
            visit_id="visit-1",
            insurance_id="OLD",
            eligibility_status="active",
            eligibility_checked_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )
    monkeypatch.setattr(agent_wiring, "get_visit_memory", lambda: fake_memory)

    agent_wiring.bind_visit_context("visit-1", patient_id=7)

    stored = fake_memory.get("visit-1")
    assert stored.patient_id == 7
    assert stored.insurance_id == "OLD"  # untouched
    assert stored.eligibility_status == "active"  # untouched


def test_bind_visit_context_is_a_noop_when_nothing_is_given(monkeypatch):
    fake_memory = _FakeVisitMemory()
    monkeypatch.setattr(agent_wiring, "get_visit_memory", lambda: fake_memory)

    agent_wiring.bind_visit_context("visit-1")

    assert fake_memory.put_calls == 0


# --- w-9-2-planner P1a: coverage_on_file binding -----------------------------


def test_bind_visit_context_stores_the_coverage_on_file_snapshot(monkeypatch):
    fake_memory = _FakeVisitMemory()
    monkeypatch.setattr(agent_wiring, "get_visit_memory", lambda: fake_memory)

    agent_wiring.bind_visit_context(
        "visit-1",
        patient_id=1737,
        insurance_id="MEM1",
        coverage_on_file={
            "payer_name": "Kaiser",
            "plan_type": "HMO",
            "member_id_masked": "******5591",
            "status": "active",
            "verified_at": "2026-03-05T08:55:00+00:00",
        },
    )

    stored = fake_memory.get("visit-1")
    assert stored.coverage_payer_name == "Kaiser"
    assert stored.coverage_plan_type == "HMO"
    assert stored.coverage_member_id_masked == "******5591"
    assert stored.coverage_status == "active"
    assert stored.coverage_verified_at.isoformat() == "2026-03-05T08:55:00+00:00"


def test_bind_visit_context_with_only_coverage_on_file_is_not_a_noop(monkeypatch):
    # coverage_on_file alone (no patient_id/insurance_id) must still bind —
    # the no-op guard must check all three fields, not just the first two.
    fake_memory = _FakeVisitMemory()
    monkeypatch.setattr(agent_wiring, "get_visit_memory", lambda: fake_memory)

    agent_wiring.bind_visit_context("visit-1", coverage_on_file={"payer_name": "Kaiser", "status": "active"})

    assert fake_memory.put_calls == 1
    assert fake_memory.get("visit-1").coverage_payer_name == "Kaiser"


def test_bind_visit_context_coverage_on_file_does_not_clobber_other_fields(monkeypatch):
    fake_memory = _FakeVisitMemory()
    fake_memory.put(
        VisitContext(
            visit_id="visit-1",
            insurance_id="OLD",
            eligibility_status="active",
            eligibility_checked_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    )
    monkeypatch.setattr(agent_wiring, "get_visit_memory", lambda: fake_memory)

    agent_wiring.bind_visit_context("visit-1", coverage_on_file={"payer_name": "Kaiser", "status": "active"})

    stored = fake_memory.get("visit-1")
    assert stored.coverage_payer_name == "Kaiser"
    assert stored.insurance_id == "OLD"  # untouched
    assert stored.eligibility_status == "active"  # untouched — a stored snapshot, not a verification


def test_bind_visit_context_explicit_none_coverage_clears_a_stale_snapshot(monkeypatch):
    # w-9-2-planner P1a review fix (B2-stale-snapshot): the gateway sends an
    # explicit coverage_on_file=None when a patient's coverage has been
    # removed. That must CLEAR the previously bound coverage_* fields, not be
    # treated the same as "coverage_on_file wasn't passed" (which must leave
    # them untouched — see the omitted-kwarg tests above).
    fake_memory = _FakeVisitMemory()
    monkeypatch.setattr(agent_wiring, "get_visit_memory", lambda: fake_memory)

    agent_wiring.bind_visit_context(
        "visit-1",
        patient_id=1737,
        insurance_id="MEM1",
        coverage_on_file={"payer_name": "Kaiser", "plan_type": "HMO", "status": "active"},
    )
    assert fake_memory.get("visit-1").coverage_payer_name == "Kaiser"

    agent_wiring.bind_visit_context("visit-1", patient_id=1737, insurance_id=None, coverage_on_file=None)

    stored = fake_memory.get("visit-1")
    assert stored.coverage_payer_name is None
    assert stored.coverage_plan_type is None
    assert stored.coverage_member_id_masked is None
    assert stored.coverage_status is None
    assert stored.coverage_verified_at is None
    assert stored.patient_id == 1737  # untouched by the clear
