"""Contract-parity tests for the two switchable AgentRuntime implementations
(libs/eligibility_agent/runtimes/{raw_bedrock,langchain_runtime}.py).

The SAME test functions run against both runtimes via the `runtime_name`
fixture, using a fake, scripted model and a fake VisitMemoryPort — no live
provider, no network, and (per the approved plan) no real boto3 or
langchain/langgraph install required:

  * raw_bedrock: driven by a FakeToolCapableModel implementing the
    ToolCapableModel port directly — no fake sys.modules needed, since
    RawBedrockAgentRuntime takes its model by dependency injection.
  * langchain: langchain_core/langgraph are faked via sys.modules (mirrors
    tests/test_bedrock_provider.py's established fake-boto3 pattern), with a
    small self-authored StateGraph/conditional-edges double that drives the
    real control-flow code in runtimes/langchain_runtime.py against a
    documented-shape fake of LangGraph's API — it validates this module's own
    control flow, not compatibility with the real library (see that module's
    docstring).

A shared "script" — a plain list of ("tool_call", args) / ("tool_call_named",
name, args) / ("text", reply) / ("error", exc) steps — drives both runtimes'
fakes identically, so passing tests are evidence the *contract* holds for
each implementation, not just that each does something plausible on its own.
"""
import logging
import sys
import types
from datetime import datetime, timezone

import httpx
import pytest

from libs.eligibility_agent.bedrock_tool_port import ConverseTurn, ToolCall, ToolCapableModel
from libs.eligibility_agent.contracts import EligibilityStatus, TerminationReason, VisitContext
from libs.eligibility_agent.eligibility_tool import TOOL_NAME
from libs.eligibility_agent.memory import VisitMemoryPort
from libs.eligibility_agent.runtimes.raw_bedrock import RawBedrockAgentRuntime
from libs.llm_client.errors import ProviderCallError, ProviderTimeoutError

# --------------------------------------------------------------------------- #
# Shared test doubles
# --------------------------------------------------------------------------- #


class FakeVisitMemory(VisitMemoryPort):
    """In-memory double, shared across separate runtime *instances* in a test
    to simulate a process restart while the backing store (Redis in
    production) survives."""

    def __init__(self):
        self._store = {}

    def get(self, visit_id):
        return self._store.get(visit_id)

    def put(self, context):
        self._store[context.visit_id] = context


def _seed_context(memory, visit_id="visit-1", insurance_id="BCBS1"):
    memory.put(VisitContext(visit_id=visit_id, insurance_id=insurance_id, updated_at=_now()))


def _now():
    return datetime.now(timezone.utc)


def eligibility_transport(status: str, checked_at: str = "2026-07-17T12:00:00Z"):
    def handler(request):
        return httpx.Response(200, json={"status": status, "checked_at": checked_at})

    return httpx.MockTransport(handler)


def _visit_memory_checked_at(memory, visit_id):
    return memory.get(visit_id).eligibility_checked_at


def never_called_transport():
    def handler(request):
        raise AssertionError("eligibility-service must not be called for this script")

    return httpx.MockTransport(handler)


class FakeToolCapableModel(ToolCapableModel):
    """Drives RawBedrockAgentRuntime with a scripted sequence of turns."""

    def __init__(self, script):
        self._script = iter(script)

    def converse(self, messages, tools, *, timeout):
        step = next(self._script)
        kind = step[0]
        if kind == "error":
            raise step[1]
        if kind == "text":
            return ConverseTurn(text=step[1], tool_calls=[])
        if kind == "tool_call":
            return ConverseTurn(text=None, tool_calls=[ToolCall(id="t1", name=TOOL_NAME, arguments=step[1])])
        if kind == "tool_call_named":
            return ConverseTurn(text=None, tool_calls=[ToolCall(id="t1", name=step[1], arguments=step[2])])
        raise AssertionError(f"unknown script step: {step!r}")


class _FakeAIMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeBoundModel:
    """Drives LangChainAgentRuntime with the SAME script shape as
    FakeToolCapableModel above."""

    def __init__(self, script):
        self._script = iter(script)

    def invoke(self, messages):
        step = next(self._script)
        kind = step[0]
        if kind == "error":
            raise step[1]
        if kind == "text":
            return _FakeAIMessage(content=step[1], tool_calls=[])
        if kind == "tool_call":
            return _FakeAIMessage(tool_calls=[{"name": TOOL_NAME, "args": step[1], "id": "t1"}])
        if kind == "tool_call_named":
            return _FakeAIMessage(tool_calls=[{"name": step[1], "args": step[2], "id": "t1"}])
        raise AssertionError(f"unknown script step: {step!r}")


class _FakeChatModel:
    def __init__(self, script):
        self._script = script

    def bind_tools(self, tools):
        return _FakeBoundModel(self._script)


def _install_fake_langgraph(monkeypatch):
    """Registers fake langchain_core/langgraph modules in sys.modules so
    runtimes/langchain_runtime.py's lazy, in-method imports resolve to test
    doubles — mirrors tests/test_bedrock_provider.py's _install_fake_boto3.
    """

    class _FakeHumanMessage:
        def __init__(self, content):
            self.content = content
            self.tool_calls = None

    class _FakeToolMessage:
        def __init__(self, content, tool_call_id):
            self.content = content
            self.tool_call_id = tool_call_id

    fake_messages_mod = types.ModuleType("langchain_core.messages")
    fake_messages_mod.HumanMessage = _FakeHumanMessage
    fake_messages_mod.ToolMessage = _FakeToolMessage
    fake_core_mod = types.ModuleType("langchain_core")

    _END = object()  # sentinel distinct from any real node-name string

    class _FakeStateGraph:
        """A minimal double of LangGraph's documented StateGraph/conditional-
        edges API shape: nodes, one entry point, and conditional-edge
        dispatch to another node or END. Enough to drive the real 2-node
        agent/tools graph built in langchain_runtime.py; not a reimplementation
        of LangGraph itself."""

        def __init__(self, schema):
            self._nodes = {}
            self._entry = None
            self._cond_edges = {}

        def add_node(self, name, fn):
            self._nodes[name] = fn

        def set_entry_point(self, name):
            self._entry = name

        def add_conditional_edges(self, source, router, mapping):
            self._cond_edges[source] = (router, mapping)

        def compile(self, checkpointer=None):
            nodes, entry, cond_edges = self._nodes, self._entry, self._cond_edges

            class _FakeCompiledGraph:
                def invoke(self, state, config=None):
                    current = entry
                    while True:
                        state = nodes[current](state)
                        router, mapping = cond_edges[current]
                        target = mapping[router(state)]
                        if target is _END:
                            return state
                        current = target

            return _FakeCompiledGraph()

    fake_graph_mod = types.ModuleType("langgraph.graph")
    fake_graph_mod.StateGraph = _FakeStateGraph
    fake_graph_mod.END = _END
    fake_langgraph_mod = types.ModuleType("langgraph")

    monkeypatch.setitem(sys.modules, "langchain_core", fake_core_mod)
    monkeypatch.setitem(sys.modules, "langchain_core.messages", fake_messages_mod)
    monkeypatch.setitem(sys.modules, "langgraph", fake_langgraph_mod)
    monkeypatch.setitem(sys.modules, "langgraph.graph", fake_graph_mod)


def build_runtime(runtime_name, *, script, memory, tool_transport, monkeypatch, max_turns=4):
    if runtime_name == "raw_bedrock":
        return RawBedrockAgentRuntime(
            memory=memory,
            model=FakeToolCapableModel(script),
            max_turns=max_turns,
            tool_transport=tool_transport,
        )
    if runtime_name == "langchain":
        _install_fake_langgraph(monkeypatch)
        from libs.eligibility_agent.runtimes.langchain_runtime import LangChainAgentRuntime

        return LangChainAgentRuntime(
            memory=memory,
            max_turns=max_turns,
            tool_transport=tool_transport,
            chat_model_factory=lambda: _FakeChatModel(script),
            checkpointer_factory=lambda: object(),  # never inspected by the fake graph
        )
    raise AssertionError(f"unknown runtime_name: {runtime_name!r}")


@pytest.fixture(params=["raw_bedrock", "langchain"])
def runtime_name(request):
    return request.param


# --------------------------------------------------------------------------- #
# Contract tests — run once per runtime_name
# --------------------------------------------------------------------------- #


def test_no_tool_call_returns_the_models_answer_directly(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    script = [("text", "Hello, how can I help?")]
    runtime = build_runtime(
        runtime_name, script=script, memory=memory, tool_transport=never_called_transport(), monkeypatch=monkeypatch
    )

    result = runtime.handle_message("visit-1", "hi")

    assert result.termination_reason == TerminationReason.ANSWERED
    assert result.tool_called is False
    assert result.turns_used == 1
    assert result.reply == "Hello, how can I help?"


def test_single_tool_call_reports_active_and_persists_it(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory)
    script = [("tool_call", {}), ("text", "You're covered.")]
    runtime = build_runtime(
        runtime_name,
        script=script,
        memory=memory,
        tool_transport=eligibility_transport("active"),
        monkeypatch=monkeypatch,
    )

    result = runtime.handle_message("visit-1", "am I covered?")

    assert result.tool_called is True
    assert result.eligibility_status == EligibilityStatus.ACTIVE
    assert result.termination_reason == TerminationReason.ANSWERED
    assert result.turns_used == 2
    assert memory.get("visit-1").eligibility_status == EligibilityStatus.ACTIVE


def test_pending_status_is_surfaced_not_silently_dropped(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory)
    script = [("tool_call", {}), ("text", "Still checking, one moment.")]
    runtime = build_runtime(
        runtime_name,
        script=script,
        memory=memory,
        tool_transport=eligibility_transport("pending"),
        monkeypatch=monkeypatch,
    )

    result = runtime.handle_message("visit-1", "check now")

    assert result.eligibility_status == EligibilityStatus.PENDING


def test_stale_status_is_surfaced_not_reported_as_fresh(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory)
    script = [("tool_call", {}), ("text", "Here's the last known result.")]
    runtime = build_runtime(
        runtime_name,
        script=script,
        memory=memory,
        tool_transport=eligibility_transport("stale"),
        monkeypatch=monkeypatch,
    )

    result = runtime.handle_message("visit-1", "check now")

    assert result.eligibility_status == EligibilityStatus.STALE


def test_malformed_tool_arguments_are_rejected_before_any_network_call(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory)
    script = [("tool_call", {"insurance_id": "smuggled-999"}), ("text", "Sorry, something went wrong.")]
    runtime = build_runtime(
        runtime_name, script=script, memory=memory, tool_transport=never_called_transport(), monkeypatch=monkeypatch
    )

    result = runtime.handle_message("visit-1", "check a different member id")

    assert result.tool_called is False
    assert result.eligibility_status is None
    assert result.termination_reason == TerminationReason.ANSWERED


def test_unknown_tool_name_is_rejected(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory)
    script = [("tool_call_named", "delete_patient_record", {}), ("text", "I can't do that.")]
    runtime = build_runtime(
        runtime_name, script=script, memory=memory, tool_transport=never_called_transport(), monkeypatch=monkeypatch
    )

    result = runtime.handle_message("visit-1", "delete my record")

    assert result.tool_called is False
    assert result.termination_reason == TerminationReason.ANSWERED


def test_repeated_tool_call_loop_is_bounded_by_max_turns(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory)
    max_turns = 3
    script = [("tool_call", {})] * max_turns  # the model never stops asking
    runtime = build_runtime(
        runtime_name,
        script=script,
        memory=memory,
        tool_transport=eligibility_transport("active"),
        monkeypatch=monkeypatch,
        max_turns=max_turns,
    )

    result = runtime.handle_message("visit-1", "keep checking")

    assert result.termination_reason == TerminationReason.MAX_TURNS
    assert result.turns_used == max_turns
    assert "try again" in result.reply.lower()


def test_provider_timeout_produces_a_safe_reply_and_never_raises(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory)
    script = [("error", ProviderTimeoutError("boom"))]
    runtime = build_runtime(
        runtime_name, script=script, memory=memory, tool_transport=never_called_transport(), monkeypatch=monkeypatch
    )

    result = runtime.handle_message("visit-1", "check now")

    assert result.termination_reason == TerminationReason.PROVIDER_ERROR
    assert "try again" in result.reply.lower()


def test_stale_result_preserves_the_original_checked_at_not_now(runtime_name, monkeypatch):
    # A payer-outage stale fallback carries its ORIGINAL (old) checked_at.
    # The runtime must persist that, never stamp "now" — otherwise a stale
    # result looks freshly verified in memory / audit views.
    memory = FakeVisitMemory()
    _seed_context(memory)
    original = "2020-01-01T00:00:00Z"
    script = [("tool_call", {}), ("text", "Showing last known result.")]
    runtime = build_runtime(
        runtime_name,
        script=script,
        memory=memory,
        tool_transport=eligibility_transport("stale", checked_at=original),
        monkeypatch=monkeypatch,
    )

    result = runtime.handle_message("visit-1", "check now")

    assert result.eligibility_status == EligibilityStatus.STALE
    stored = _visit_memory_checked_at(memory, "visit-1")
    assert stored == datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    # And definitively NOT "now": 2020 is years before this test runs.
    assert stored.year == 2020


def test_failed_check_preserves_prior_checked_at_rather_than_stamping_now(runtime_name, monkeypatch):
    # Seed a prior ACTIVE verification at a known old time, then a check that
    # comes back UNKNOWN (no as_of). The prior timestamp must be preserved,
    # not overwritten with "now".
    memory = FakeVisitMemory()
    prior = datetime(2021, 6, 6, 6, 0, 0, tzinfo=timezone.utc)
    memory.put(
        VisitContext(
            visit_id="visit-1",
            insurance_id="BCBS1",
            eligibility_status=EligibilityStatus.ACTIVE,
            eligibility_checked_at=prior,
            updated_at=prior,
        )
    )
    # eligibility-service reachable but returns unknown with no checked_at.
    def handler(request):
        return httpx.Response(200, json={"status": "unknown"})

    runtime = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "Couldn't verify right now.")],
        memory=memory,
        tool_transport=httpx.MockTransport(handler),
        monkeypatch=monkeypatch,
    )

    runtime.handle_message("visit-1", "check now")

    assert _visit_memory_checked_at(memory, "visit-1") == prior


def test_non_retryable_provider_error_becomes_a_safe_result_not_an_exception(runtime_name, monkeypatch):
    # A non-retryable provider failure (e.g. AccessDenied, normalized by the
    # tool port to ProviderCallError) must degrade to a safe PROVIDER_ERROR
    # turn, never escape handle_message.
    memory = FakeVisitMemory()
    _seed_context(memory)
    script = [("error", ProviderCallError("AccessDeniedException"))]
    runtime = build_runtime(
        runtime_name, script=script, memory=memory, tool_transport=never_called_transport(), monkeypatch=monkeypatch
    )

    result = runtime.handle_message("visit-1", "check now")

    assert result.termination_reason == TerminationReason.PROVIDER_ERROR
    assert "try again" in result.reply.lower()


def test_two_visits_do_not_leak_into_each_others_context(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory, visit_id="visit-A", insurance_id="AAA111")
    _seed_context(memory, visit_id="visit-B", insurance_id="BBB222")

    runtime_a = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "A is covered.")],
        memory=memory,
        tool_transport=eligibility_transport("active"),
        monkeypatch=monkeypatch,
    )
    result_a = runtime_a.handle_message("visit-A", "check A")

    runtime_b = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "B is not covered.")],
        memory=memory,
        tool_transport=eligibility_transport("inactive"),
        monkeypatch=monkeypatch,
    )
    result_b = runtime_b.handle_message("visit-B", "check B")

    assert result_a.eligibility_status == EligibilityStatus.ACTIVE
    assert result_b.eligibility_status == EligibilityStatus.INACTIVE
    assert memory.get("visit-A").eligibility_status == EligibilityStatus.ACTIVE
    assert memory.get("visit-B").eligibility_status == EligibilityStatus.INACTIVE


def test_restart_resume_a_fresh_runtime_instance_sees_prior_persisted_status(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory, visit_id="visit-1")

    first = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "You're covered.")],
        memory=memory,
        tool_transport=eligibility_transport("active"),
        monkeypatch=monkeypatch,
    )
    first.handle_message("visit-1", "check now")

    # Simulate a process restart: a brand-new runtime instance, but the SAME
    # backing memory store (Redis in production). The model doesn't call the
    # tool again this turn, yet the prior status must still surface.
    second = build_runtime(
        runtime_name,
        script=[("text", "Sure, anything else?")],
        memory=memory,
        tool_transport=never_called_transport(),
        monkeypatch=monkeypatch,
    )
    result = second.handle_message("visit-1", "anything else?")

    assert result.tool_called is False
    assert result.eligibility_status == EligibilityStatus.ACTIVE


def test_phi_is_never_logged_even_when_the_eligibility_call_fails(runtime_name, monkeypatch, caplog):
    memory = FakeVisitMemory()
    secret_member_id = "SECRET-MEMBER-42"
    _seed_context(memory, visit_id="visit-1", insurance_id=secret_member_id)

    def handler(request):
        raise httpx.ConnectError(f"connection refused for member {secret_member_id}", request=request)

    runtime = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "Let me check again shortly.")],
        memory=memory,
        tool_transport=httpx.MockTransport(handler),
        monkeypatch=monkeypatch,
    )

    with caplog.at_level(logging.WARNING):
        result = runtime.handle_message("visit-1", "check now")

    assert result.eligibility_status == EligibilityStatus.UNKNOWN
    for record in caplog.records:
        assert secret_member_id not in record.getMessage()
