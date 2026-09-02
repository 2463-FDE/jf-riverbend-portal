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

from libs.eligibility_agent.bedrock_tool_port import (
    ConverseStreamEvent,
    ConverseTurn,
    ToolCall,
    ToolCapableModel,
)
from libs.eligibility_agent.contracts import EligibilityStatus, TerminationReason, VisitContext
from libs.eligibility_agent.eligibility_tool import COVERAGE_TOOL_NAME, VERIFY_TOOL_NAME, EligibilityToolConfig
from libs.eligibility_agent.memory import VisitMemoryPort
from libs.eligibility_agent.runtimes.raw_bedrock import _SAFE_PROVIDER_ERROR_REPLY, RawBedrockAgentRuntime
from libs.llm_client.errors import ProviderCallError, ProviderTimeoutError

# w-9-2-planner P1a: every test in this file that scripts a bare ("tool_call",
# args) step is exercising verify_current_eligibility's LIVE-CALL mechanics
# (response parsing, persistence, error handling) against a mocked transport
# — payer_configured=True keeps that path reachable, the same way it already
# was before the tool split. The payer_configured=False / simulated /
# get_coverage_on_file behavior gets its own dedicated tests below.
_CONFIGURED = EligibilityToolConfig(payer_configured=True)

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


def _seed_context(memory, visit_id="visit-1", insurance_id="BCBS1", **coverage_fields):
    memory.put(VisitContext(visit_id=visit_id, insurance_id=insurance_id, updated_at=_now(), **coverage_fields))


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
    """Drives RawBedrockAgentRuntime with a scripted sequence of turns.

    `offered_tools`, when given, collects the tool-name list the runtime
    offered on each provider call — the only way a test can see what the
    model was even able to reach for."""

    def __init__(self, script, offered_tools=None):
        self._script = iter(script)
        self._offered_tools = offered_tools

    def converse(self, messages, tools, *, timeout):
        if self._offered_tools is not None:
            self._offered_tools.append([spec["name"] for spec in tools])
        step = next(self._script)
        kind = step[0]
        if kind == "error":
            raise step[1]
        if kind == "text":
            return ConverseTurn(text=step[1], tool_calls=[])
        if kind == "tool_call":
            return ConverseTurn(text=None, tool_calls=[ToolCall(id="t1", name=VERIFY_TOOL_NAME, arguments=step[1])])
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
            return _FakeAIMessage(tool_calls=[{"name": VERIFY_TOOL_NAME, "args": step[1], "id": "t1"}])
        if kind == "tool_call_named":
            return _FakeAIMessage(tool_calls=[{"name": step[1], "args": step[2], "id": "t1"}])
        raise AssertionError(f"unknown script step: {step!r}")


class _FakeChatModel:
    def __init__(self, script, offered_tools=None):
        self._script = script
        self._offered_tools = offered_tools

    def bind_tools(self, tools):
        # The langchain equivalent of FakeToolCapableModel's record above:
        # bind_tools is where that runtime decides what the model may call.
        if self._offered_tools is not None:
            self._offered_tools.append([spec["name"] for spec in tools])
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


def build_runtime(
    runtime_name, *, script, memory, tool_transport, monkeypatch, max_turns=4, tool_config=_CONFIGURED,
    offered_tools=None,
):
    if runtime_name == "raw_bedrock":
        return RawBedrockAgentRuntime(
            memory=memory,
            model=FakeToolCapableModel(script, offered_tools=offered_tools),
            max_turns=max_turns,
            tool_config=tool_config,
            tool_transport=tool_transport,
        )
    if runtime_name == "langchain":
        _install_fake_langgraph(monkeypatch)
        from libs.eligibility_agent.runtimes.langchain_runtime import LangChainAgentRuntime

        return LangChainAgentRuntime(
            memory=memory,
            max_turns=max_turns,
            tool_config=tool_config,
            tool_transport=tool_transport,
            chat_model_factory=lambda: _FakeChatModel(script, offered_tools=offered_tools),
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

    # w-9-2-planner P1a: a failed/unavailable attempt is not itself learned
    # information — it must not overwrite the (here, absent) prior status
    # with "unknown", which is exactly the "a new verification never
    # upgrades stored data" boundary applied to the no-prior-data case.
    assert result.eligibility_status is None
    for record in caplog.records:
        assert secret_member_id not in record.getMessage()


# --- w-9-2-planner P1a: get_coverage_on_file, and the outcome split --------


def test_get_coverage_on_file_answers_from_the_stored_snapshot_with_no_network_call(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(
        memory,
        coverage_payer_name="Kaiser",
        coverage_plan_type="HMO",
        coverage_member_id_masked="******5591",
        coverage_status="active",
        coverage_verified_at=datetime(2026, 3, 5, 8, 55, 0, tzinfo=timezone.utc),
    )
    script = [("tool_call_named", COVERAGE_TOOL_NAME, {}), ("text", "You have Kaiser HMO on file, active.")]
    runtime = build_runtime(
        runtime_name, script=script, memory=memory, tool_transport=never_called_transport(), monkeypatch=monkeypatch
    )

    result = runtime.handle_message("visit-1", "what coverage do you have on file?")

    assert result.tool_called is True
    assert result.termination_reason == TerminationReason.ANSWERED


def test_get_coverage_on_file_never_touches_eligibility_status_or_persists(runtime_name, monkeypatch):
    # The stored snapshot is a pure read — it must never be mistaken for a
    # fresh verification outcome, in either the turn result or memory.
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_payer_name="Kaiser", coverage_status="active")
    script = [("tool_call_named", COVERAGE_TOOL_NAME, {}), ("text", "Here's what's on file.")]
    runtime = build_runtime(
        runtime_name, script=script, memory=memory, tool_transport=never_called_transport(), monkeypatch=monkeypatch
    )

    result = runtime.handle_message("visit-1", "what's on file?")

    assert result.eligibility_status is None  # never set by this tool
    assert memory.get("visit-1").eligibility_status is None


def test_unconfigured_payer_reports_simulated_and_does_not_persist_active(runtime_name, monkeypatch):
    # w-9-2-planner P1a's core boundary: a simulated attempt must never
    # upgrade stored/absent data into "active" in memory, even though the
    # tool's own note may mention the stored status.
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_status="active")
    script = [("tool_call", {}), ("text", "This is a synthetic training environment.")]
    runtime = build_runtime(
        runtime_name,
        script=script,
        memory=memory,
        tool_transport=never_called_transport(),
        monkeypatch=monkeypatch,
        tool_config=EligibilityToolConfig(payer_configured=False),
    )

    result = runtime.handle_message("visit-1", "check now")

    assert result.tool_called is True
    assert result.eligibility_status is None  # not persisted as a fresh ACTIVE result
    assert memory.get("visit-1").eligibility_status is None
    assert memory.get("visit-1").coverage_status == "active"  # the stored snapshot itself is untouched


def test_a_prior_verified_status_survives_an_unavailable_reattempt(runtime_name, monkeypatch):
    # Seed a prior genuine verification, then a live attempt that fails
    # outright (transport error) — the PRIOR verified result must survive,
    # not be blanked out by the failed reattempt.
    memory = FakeVisitMemory()
    prior = _now()
    memory.put(
        VisitContext(
            visit_id="visit-1",
            insurance_id="BCBS1",
            eligibility_status=EligibilityStatus.ACTIVE,
            eligibility_checked_at=prior,
            updated_at=prior,
        )
    )

    def handler(request):
        raise httpx.ConnectError("payer unreachable", request=request)

    script = [("tool_call", {}), ("text", "Verification unavailable right now.")]
    runtime = build_runtime(
        runtime_name, script=script, memory=memory, tool_transport=httpx.MockTransport(handler), monkeypatch=monkeypatch
    )

    result = runtime.handle_message("visit-1", "check again")

    assert result.eligibility_status == EligibilityStatus.ACTIVE
    assert memory.get("visit-1").eligibility_status == EligibilityStatus.ACTIVE


# --- w-9-2-planner P1b: handle_message_stream (raw_bedrock only — langchain --
# is a comparison spike never wired into a running service, see that
# module's own docstring) --------------------------------------------------
#
# FakeToolCapableModel doesn't override converse_stream, so it runs through
# ToolCapableModel's own default fallback (one chunk per turn from a
# blocking converse() call) — sufficient to test the RUNTIME's own loop,
# dispatch, persistence-scoping, and termination behavior; token-level
# chunking itself is bedrock_tool_port.py's concern, covered in
# test_bedrock_tool_port.py.


def _stream_runtime(script, memory, tool_transport, max_turns=4, tool_config=_CONFIGURED):
    return RawBedrockAgentRuntime(
        memory=memory,
        model=FakeToolCapableModel(script),
        max_turns=max_turns,
        tool_config=tool_config,
        tool_transport=tool_transport,
    )


def test_stream_forwards_the_final_answer_as_delta_events_then_one_done_event():
    memory = FakeVisitMemory()
    runtime = _stream_runtime([("text", "Hello there")], memory, never_called_transport())

    events = list(runtime.handle_message_stream("visit-1", "hi"))

    assert [e.kind for e in events] == ["delta", "done"]
    assert events[0].text == "Hello there"
    assert events[1].termination_reason == TerminationReason.ANSWERED
    assert events[1].turns_used == 1


def test_stream_never_forwards_a_tool_call_as_a_delta_and_persists_a_verified_outcome():
    memory = FakeVisitMemory()
    _seed_context(memory)
    runtime = _stream_runtime(
        [("tool_call", {}), ("text", "You're covered.")], memory, eligibility_transport("active")
    )

    events = list(runtime.handle_message_stream("visit-1", "am I covered?"))

    deltas = [e for e in events if e.kind == "delta"]
    # The tool call itself never appears as a delta, and now that a tool HAS
    # run the single delta is the deterministic render of its payload — the
    # scripted model prose ("You're covered.") is deliberately discarded.
    assert [d.text for d in deltas] == ["Eligibility is active as of July 17, 2026."]
    done = events[-1]
    assert done.kind == "done"
    assert done.eligibility_status == EligibilityStatus.ACTIVE
    assert memory.get("visit-1").eligibility_status == EligibilityStatus.ACTIVE


def test_stream_get_coverage_on_file_never_updates_eligibility_status():
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_payer_name="Kaiser", coverage_status="active")
    runtime = _stream_runtime(
        [("tool_call_named", COVERAGE_TOOL_NAME, {}), ("text", "Here's what's on file.")],
        memory,
        never_called_transport(),
    )

    events = list(runtime.handle_message_stream("visit-1", "what's on file?"))

    done = events[-1]
    assert done.kind == "done"
    assert done.eligibility_status is None
    assert memory.get("visit-1").eligibility_status is None


def test_stream_provider_error_emits_exactly_one_error_event_never_a_partial_answer():
    memory = FakeVisitMemory()
    _seed_context(memory)
    runtime = _stream_runtime([("error", ProviderTimeoutError("boom"))], memory, never_called_transport())

    events = list(runtime.handle_message_stream("visit-1", "check now"))

    assert len(events) == 1
    assert events[0].kind == "error"
    assert "try again" in events[0].text.lower()
    assert events[0].termination_reason == TerminationReason.PROVIDER_ERROR


def test_stream_bounded_by_max_turns_emits_done_with_max_turns_reason():
    memory = FakeVisitMemory()
    _seed_context(memory)
    max_turns = 3
    script = [("tool_call", {})] * max_turns
    runtime = _stream_runtime(script, memory, eligibility_transport("active"), max_turns=max_turns)

    events = list(runtime.handle_message_stream("visit-1", "keep checking"))

    # w-9-2-planner P1b review fix (STREAM-MAX-TURNS-BLANK): the streaming
    # path used to emit a bare "done" with no reply text at all on this
    # branch — unlike handle_message's equivalent, which returns
    # _SAFE_MAX_TURNS_REPLY as its reply. The text must arrive as a "delta"
    # (VisitStreamEvent's own contract: "done" never carries reply text).
    done = events[-1]
    assert done.kind == "done"
    assert done.termination_reason == TerminationReason.MAX_TURNS
    assert done.turns_used == max_turns
    deltas = [e for e in events if e.kind == "delta"]
    assert deltas, "expected a delta carrying the max-turns reply text"
    assert "try again" in deltas[-1].text.lower()


def test_stream_stopping_early_never_triggers_further_model_or_tool_calls():
    # Simulates a client disconnect mid-stream: the caller stops iterating
    # after the first event. A script step that would raise if reached
    # proves nothing beyond that point was ever touched.
    memory = FakeVisitMemory()
    _seed_context(memory)

    def _unreachable(*a, **k):
        raise AssertionError("must not make another model call after the caller stopped iterating")

    class _OneShotThenExplode(ToolCapableModel):
        def __init__(self):
            self._used = False

        def converse(self, messages, tools, *, timeout):
            if self._used:
                _unreachable()
            self._used = True
            return ConverseTurn(text="first chunk", tool_calls=[])

    runtime = RawBedrockAgentRuntime(
        memory=memory, model=_OneShotThenExplode(), tool_config=_CONFIGURED, tool_transport=never_called_transport()
    )

    gen = runtime.handle_message_stream("visit-1", "hi")
    first = next(gen)
    gen.close()

    assert first.kind == "delta"


# --- W10 Metrics Stage 4: centrally enforced request bound (raw_bedrock only
# — see the file-level docstring on why the stream tests above are also
# raw_bedrock-only: langchain_runtime.py is a comparison spike, never wired
# into a running service, and this preflight check is only wired into the
# default runtime) --------------------------------------------------------


def test_an_oversized_message_is_rejected_before_any_provider_call():
    from libs.agent_budget import BUDGETS

    memory = FakeVisitMemory()
    # An EMPTY script: FakeToolCapableModel.converse() raises StopIteration
    # on its very first call — proof positive the runtime never reached it.
    model = FakeToolCapableModel([])
    runtime = RawBedrockAgentRuntime(
        memory=memory, model=model, tool_config=_CONFIGURED, tool_transport=never_called_transport(),
    )
    oversized = "x" * (BUDGETS["eligibility_agent_chat"].max_input_chars + 1)

    result = runtime.handle_message("visit-1", oversized)

    assert result.termination_reason == TerminationReason.BUDGET_REJECTED
    assert result.turns_used == 0


def test_an_oversized_message_is_rejected_before_any_provider_call_streaming():
    from libs.agent_budget import BUDGETS

    memory = FakeVisitMemory()
    model = FakeToolCapableModel([])  # empty script — see the non-streaming test above
    runtime = RawBedrockAgentRuntime(
        memory=memory, model=model, tool_config=_CONFIGURED, tool_transport=never_called_transport(),
    )
    oversized = "x" * (BUDGETS["eligibility_agent_chat"].max_input_chars + 1)

    events = list(runtime.handle_message_stream("visit-1", oversized))

    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].termination_reason == TerminationReason.BUDGET_REJECTED


def test_a_message_within_the_bound_is_not_affected_by_the_preflight_check():
    memory = FakeVisitMemory()
    model = FakeToolCapableModel([("text", "Hello there")])
    runtime = RawBedrockAgentRuntime(
        memory=memory, model=model, tool_config=_CONFIGURED, tool_transport=never_called_transport(),
    )

    result = runtime.handle_message("visit-1", "a short message")

    assert result.termination_reason == TerminationReason.ANSWERED


# --- Response contract (raw_bedrock only — the default runtime; langchain
# remains the unwired comparison spike, see the file-level docstring) -------
#
# These prove the two things the model no longer decides: which tool it may
# reach for a given ask, and what the caller reads once that tool has run.


def _contract_runtime(script, memory, tool_transport, tool_config=_CONFIGURED):
    """raw_bedrock wired with the shared offered-tools recorder; returns the
    runtime and the list of tool-name lists it offered per provider call."""
    offered = []
    runtime = RawBedrockAgentRuntime(
        memory=memory,
        model=FakeToolCapableModel(script, offered_tools=offered),
        tool_config=tool_config,
        tool_transport=tool_transport,
    )
    return runtime, offered


def test_a_verification_ask_only_ever_offers_the_verify_tool():
    memory = FakeVisitMemory()
    _seed_context(memory)
    runtime, offered = _contract_runtime(
        [("tool_call", {}), ("text", "ignored")], memory, eligibility_transport("active")
    )

    runtime.handle_message("visit-1", "Is insurance valid?")

    assert offered[0] == [VERIFY_TOOL_NAME]


def test_a_stored_record_ask_only_ever_offers_the_coverage_tool():
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_payer_name="Kaiser", coverage_plan_type="HMO", coverage_status="active")
    runtime, offered = _contract_runtime(
        [("tool_call_named", COVERAGE_TOOL_NAME, {}), ("text", "ignored")], memory, never_called_transport()
    )

    runtime.handle_message("visit-1", "What coverage is on file?")

    assert offered[0] == [COVERAGE_TOOL_NAME]


def test_an_explicitly_combined_ask_offers_both_tools():
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_payer_name="Kaiser", coverage_status="active")
    runtime, offered = _contract_runtime(
        [("tool_call_named", COVERAGE_TOOL_NAME, {}), ("text", "ignored")], memory, never_called_transport()
    )

    runtime.handle_message("visit-1", "What coverage is on file, and is it still active?")

    assert set(offered[0]) == {VERIFY_TOOL_NAME, COVERAGE_TOOL_NAME}


def test_a_tool_outside_the_requested_intent_is_refused_at_dispatch():
    """Narrowing the offer is not enough on its own — a model can still name
    a tool it was never offered, so the same narrowing is enforced where the
    tool would actually run. never_called_transport() proves no payer call
    escaped."""
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_payer_name="Kaiser", coverage_status="active")
    runtime, _ = _contract_runtime(
        [("tool_call", {}), ("text", "I could not do that.")], memory, never_called_transport()
    )

    result = runtime.handle_message("visit-1", "What coverage is on file?")

    assert result.tool_called is False
    assert result.eligibility_status is None
    assert memory.get("visit-1").eligibility_status is None
    # No tool ran, so there is nothing to render deterministically and the
    # model's own safe text is what the caller sees.
    assert result.reply == "I could not do that."


def test_a_verified_result_is_rendered_deterministically_not_by_the_model():
    memory = FakeVisitMemory()
    _seed_context(memory)
    runtime, _ = _contract_runtime(
        [("tool_call", {}), ("text", "# Eligibility ✅\n\n| Field | Value |\n|---|---|\n| Status | ACTIVE |")],
        memory,
        eligibility_transport("active", checked_at="2026-08-23T14:02:00Z"),
    )

    result = runtime.handle_message("visit-1", "verify eligibility")

    assert result.reply == "Eligibility is active as of August 23, 2026."
    assert result.eligibility_status == EligibilityStatus.ACTIVE


def test_a_simulated_outcome_never_reads_as_a_completed_verification():
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_status="active")
    runtime, _ = _contract_runtime(
        [("tool_call", {}), ("text", "Verified! The patient's insurance is active. 🎉")],
        memory,
        never_called_transport(),
        tool_config=EligibilityToolConfig(payer_configured=False),
    )

    result = runtime.handle_message("visit-1", "verify eligibility")

    assert result.reply == (
        "A current eligibility check was not run because this is a synthetic training environment. "
        "Coverage on file is active."
    )
    # A simulated attempt must not be recorded as a fresh verification.
    assert memory.get("visit-1").eligibility_status is None


def test_an_unavailable_outcome_does_not_overwrite_stored_coverage():
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_status="active")

    def failing(request):
        raise httpx.ConnectError("payer unreachable")

    runtime, _ = _contract_runtime(
        [("tool_call", {}), ("text", "ignored")], memory, httpx.MockTransport(failing)
    )

    result = runtime.handle_message("visit-1", "verify eligibility")

    assert result.reply.startswith("Eligibility could not be verified right now.")
    assert "active" in result.reply  # the stored record is reported, and reported AS stored
    assert memory.get("visit-1").coverage_status == "active"
    assert memory.get("visit-1").eligibility_status is None


def test_a_stored_lookup_renders_concise_plain_text_without_the_member_id():
    memory = FakeVisitMemory()
    _seed_context(
        memory,
        coverage_payer_name="UnitedHealthcare",
        coverage_plan_type="HMO",
        coverage_status="active",
        coverage_member_id_masked="****6789",
    )
    runtime, _ = _contract_runtime(
        [("tool_call_named", COVERAGE_TOOL_NAME, {}), ("text", "ignored")], memory, never_called_transport()
    )

    result = runtime.handle_message("visit-1", "What coverage is on file?")

    assert result.reply == "Coverage on file is UnitedHealthcare HMO. Its stored status is active."
    assert "6789" not in result.reply


def test_stream_replaces_model_prose_with_one_deterministic_delta():
    """The streamed turn must not show the model's own description of the
    outcome first and then contradict it — the only delta is the render."""
    memory = FakeVisitMemory()
    _seed_context(memory)
    model = FakeToolCapableModel(
        [("tool_call", {}), ("text", "Sure! ✅ Here is a **table** of results | a | b |")]
    )
    runtime = RawBedrockAgentRuntime(
        memory=memory, model=model, tool_config=_CONFIGURED,
        tool_transport=eligibility_transport("active", checked_at="2026-08-23T14:02:00Z"),
    )

    events = list(runtime.handle_message_stream("visit-1", "verify eligibility"))

    deltas = [e for e in events if e.kind == "delta"]
    assert [d.text for d in deltas] == ["Eligibility is active as of August 23, 2026."]
    assert events[-1].kind == "done"
    assert events[-1].termination_reason == TerminationReason.ANSWERED
    assert len([e for e in events if e.kind in ("done", "error")]) == 1


class _PreToolProseStreamModel(ToolCapableModel):
    """Reproduces the Bedrock behaviour the buffering exists for: a single
    provider turn that emits prose BEFORE its tool call. The default
    ToolCapableModel.converse_stream fallback cannot express that ordering —
    it replays a completed ConverseTurn — so this double drives
    converse_stream directly."""

    def __init__(self):
        self.turns = 0

    def converse(self, messages, tools, *, timeout):  # pragma: no cover - unused
        raise AssertionError("this double is streaming-only")

    def converse_stream(self, messages, tools, *, timeout):
        self.turns += 1
        if self.turns == 1:
            # Prose first, then the tool call, inside ONE turn.
            yield ConverseStreamEvent(kind="text_delta", text="Eligibility is active")
            yield ConverseStreamEvent(
                kind="tool_call", tool_call=ToolCall(id="t1", name=VERIFY_TOOL_NAME, arguments={})
            )
            yield ConverseStreamEvent(kind="stop", stop_reason="tool_use")
            return
        yield ConverseStreamEvent(kind="text_delta", text="As you can see, everything looks great!")
        yield ConverseStreamEvent(kind="stop", stop_reason="end_turn")


def test_stream_never_leaks_prose_emitted_before_a_tool_call_in_the_same_turn():
    """STREAM-PRETOOL-PROSE: the model's guess at the answer is written
    before the lookup that decides whether it is true. Neither that guess nor
    its later narration may reach the browser — only the deterministic
    render of the payload."""
    memory = FakeVisitMemory()
    _seed_context(memory)
    runtime = RawBedrockAgentRuntime(
        memory=memory,
        model=_PreToolProseStreamModel(),
        tool_config=_CONFIGURED,
        tool_transport=eligibility_transport("inactive", checked_at="2026-08-23T14:02:00Z"),
    )

    events = list(runtime.handle_message_stream("visit-1", "verify eligibility"))

    deltas = [e.text for e in events if e.kind == "delta"]
    assert deltas == ["Eligibility is inactive as of August 23, 2026."]
    # The pre-tool guess said "active" while the payer said inactive — the
    # exact contradiction that reaching the browser early would cause.
    assert not any("Eligibility is active" in (text or "") for text in deltas)
    assert not any("everything looks great" in (text or "") for text in deltas)
    assert len([e for e in events if e.kind in ("done", "error")]) == 1


def test_stream_still_delivers_a_tool_free_turns_own_text():
    """The buffering must not swallow an ordinary answer: with no tool call
    anywhere, the turn's text is released in its original order."""
    memory = FakeVisitMemory()
    runtime = _stream_runtime([("text", "Hello there")], memory, never_called_transport())

    events = list(runtime.handle_message_stream("visit-1", "hello"))

    assert [e.text for e in events if e.kind == "delta"] == ["Hello there"]
    assert events[-1].kind == "done"
    assert events[-1].termination_reason == TerminationReason.ANSWERED


def test_stream_discards_buffered_text_when_the_provider_then_fails():
    """A turn that never completed cannot have produced a trustworthy
    answer, so its partial text is dropped in favour of the sanitized
    terminal error."""

    class _ProseThenFailModel(ToolCapableModel):
        def converse(self, messages, tools, *, timeout):  # pragma: no cover - unused
            raise AssertionError("streaming-only")

        def converse_stream(self, messages, tools, *, timeout):
            yield ConverseStreamEvent(kind="text_delta", text="Eligibility is active")
            raise ProviderTimeoutError("ReadTimeout")

    memory = FakeVisitMemory()
    runtime = RawBedrockAgentRuntime(
        memory=memory, model=_ProseThenFailModel(), tool_config=_CONFIGURED,
        tool_transport=never_called_transport(),
    )

    events = list(runtime.handle_message_stream("visit-1", "verify eligibility"))

    assert [e.kind for e in events] == ["error"]
    assert events[0].text == _SAFE_PROVIDER_ERROR_REPLY
    assert events[0].termination_reason == TerminationReason.PROVIDER_ERROR


# --- Response contract parity across BOTH runtimes ------------------------


def test_a_coverage_phrased_verification_ask_offers_only_the_verify_tool(runtime_name, monkeypatch):
    """INTENT-COVERED-GAP: "am I covered?" is how this is actually asked."""
    memory = FakeVisitMemory()
    _seed_context(memory)
    offered = []
    runtime = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "ignored")],
        memory=memory,
        tool_transport=eligibility_transport("active"),
        monkeypatch=monkeypatch,
        offered_tools=offered,
    )

    runtime.handle_message("visit-1", "am I covered?")

    assert offered[0] == [VERIFY_TOOL_NAME]


def test_a_coverage_lookup_is_refused_during_a_verification_only_request(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_payer_name="Kaiser", coverage_status="active")
    runtime = build_runtime(
        runtime_name,
        script=[("tool_call_named", COVERAGE_TOOL_NAME, {}), ("text", "I could not do that.")],
        memory=memory,
        tool_transport=never_called_transport(),
        monkeypatch=monkeypatch,
    )

    result = runtime.handle_message("visit-1", "am I covered?")

    assert result.tool_called is False
    # Nothing ran, so there is no payload to render and the model's own safe
    # text stands — a refused call must never be dressed up as a lookup.
    assert result.reply == "I could not do that."


def test_both_runtimes_render_a_verified_outcome_identically(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory)
    runtime = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "## Eligibility ✅\n\n| Status | ACTIVE |\n|---|---|")],
        memory=memory,
        tool_transport=eligibility_transport("active", checked_at="2026-08-23T14:02:00Z"),
        monkeypatch=monkeypatch,
    )

    result = runtime.handle_message("visit-1", "verify eligibility")

    assert result.reply == "Eligibility is active as of August 23, 2026."
    assert result.eligibility_status == EligibilityStatus.ACTIVE


def test_both_runtimes_render_a_simulated_outcome_identically(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_status="active")
    runtime = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "Verified! Insurance is active. 🎉")],
        memory=memory,
        tool_transport=never_called_transport(),
        monkeypatch=monkeypatch,
        tool_config=EligibilityToolConfig(payer_configured=False),
    )

    result = runtime.handle_message("visit-1", "am I covered?")

    assert result.reply == (
        "A current eligibility check was not run because this is a synthetic training environment. "
        "Coverage on file is active."
    )
    assert memory.get("visit-1").eligibility_status is None


def test_both_runtimes_render_an_unavailable_outcome_identically(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_status="active")

    def failing(request):
        raise httpx.ConnectError("payer unreachable")

    runtime = build_runtime(
        runtime_name,
        script=[("tool_call", {}), ("text", "All good — the patient is covered!")],
        memory=memory,
        tool_transport=httpx.MockTransport(failing),
        monkeypatch=monkeypatch,
    )

    result = runtime.handle_message("visit-1", "verify eligibility")

    assert result.reply == (
        "Eligibility could not be verified right now. "
        "The coverage record on file is active. "
        "Try again later or contact the payer."
    )
    assert memory.get("visit-1").eligibility_status is None


def test_no_markdown_or_model_narration_survives_a_successful_tool_call(runtime_name, monkeypatch):
    memory = FakeVisitMemory()
    _seed_context(memory, coverage_payer_name="Kaiser", coverage_plan_type="HMO", coverage_status="active")
    narration = "Sure! **Here** is a | table | ✅ of the coverage on file."
    runtime = build_runtime(
        runtime_name,
        script=[("tool_call_named", COVERAGE_TOOL_NAME, {}), ("text", narration)],
        memory=memory,
        tool_transport=never_called_transport(),
        monkeypatch=monkeypatch,
    )

    result = runtime.handle_message("visit-1", "What coverage is on file?")

    assert result.reply == "Coverage on file is Kaiser HMO. Its stored status is active."
    assert narration not in result.reply
    for forbidden in ("|", "**", "✅", "Sure!"):
        assert forbidden not in result.reply
