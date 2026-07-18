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
from libs.eligibility_agent.contracts import TerminationReason, VisitContext, VisitTurnResult

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
