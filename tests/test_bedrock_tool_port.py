"""Tests for the tool-capable Bedrock Converse port
(libs/eligibility_agent/bedrock_tool_port.py).

boto3/botocore are faked via sys.modules (mirrors tests/test_bedrock_provider.py)
so these never require a real install or make a real AWS call. The focus is
the error boundary: every failure that can occur after construction must leave
converse() as a member of the llm_client provider-error vocabulary
(ProviderTimeoutError / ProviderTransientError / ProviderCallError), never as a
raw botocore ClientError or a KeyError — that is what lets the runtime's single
provider-error catch guarantee it never throws a provider failure to the user.
"""
import sys
import types

import pytest

from libs.metrics import ai as ai_metrics
from libs.llm_client.errors import (
    ProviderCallError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    ProviderTransientError,
)


class _FakeClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class _FakeTimeoutError(Exception):
    pass


class _FakeEventStream:
    """A close()-able iterable, mirroring botocore's real streaming response
    body shape closely enough for converse_stream()'s own `finally: close()`
    to have something to call."""

    def __init__(self, events, stream_error=None):
        self._events = list(events)
        self._stream_error = stream_error
        self.closed = False

    def __iter__(self):
        yield from self._events
        if self._stream_error is not None:
            raise self._stream_error

    def close(self):
        self.closed = True


def _install_fake_boto3(
    monkeypatch, *, converse_result=None, converse_error=None, stream_events=None, stream_open_error=None,
    stream_iter_error=None,
):
    fake_stream = _FakeEventStream(stream_events or [], stream_error=stream_iter_error)

    class _FakeBedrockClient:
        def converse(self, **kwargs):
            if converse_error is not None:
                raise converse_error
            return converse_result

        def converse_stream(self, **kwargs):
            if stream_open_error is not None:
                raise stream_open_error
            return {"stream": fake_stream}

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service_name, **kwargs: _FakeBedrockClient()

    fake_botocore = types.ModuleType("botocore")
    fake_config_mod = types.ModuleType("botocore.config")
    fake_config_mod.Config = lambda **kwargs: kwargs
    fake_exceptions_mod = types.ModuleType("botocore.exceptions")
    fake_exceptions_mod.ClientError = _FakeClientError
    fake_exceptions_mod.ConnectTimeoutError = _FakeTimeoutError
    fake_exceptions_mod.ReadTimeoutError = _FakeTimeoutError

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", fake_config_mod)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", fake_exceptions_mod)
    return fake_stream


def _model():
    from libs.eligibility_agent.bedrock_tool_port import BedrockConverseToolModel

    return BedrockConverseToolModel(model_id="anthropic.claude-3-5-sonnet", region="us-east-1")


_TOOLS = [{"name": "check_eligibility", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


def test_missing_model_id_raises_not_configured():
    from libs.eligibility_agent.bedrock_tool_port import BedrockConverseToolModel

    with pytest.raises(ProviderNotConfiguredError):
        BedrockConverseToolModel(model_id=None, region="us-east-1")


def test_placeholder_model_id_raises_not_configured():
    from libs.eligibility_agent.bedrock_tool_port import BedrockConverseToolModel

    with pytest.raises(ProviderNotConfiguredError):
        BedrockConverseToolModel(model_id="changeme", region="us-east-1")


def test_parses_text_and_tool_calls(monkeypatch):
    response = {
        "output": {
            "message": {
                "content": [
                    {"text": "let me check"},
                    {"toolUse": {"toolUseId": "u1", "name": "check_eligibility", "input": {}}},
                ]
            }
        },
        "stopReason": "tool_use",
    }
    _install_fake_boto3(monkeypatch, converse_result=response)

    turn = _model().converse([{"role": "user", "content": [{"text": "hi"}]}], _TOOLS, timeout=10)

    assert turn.text == "let me check"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "check_eligibility"
    assert turn.tool_calls[0].id == "u1"
    assert turn.stop_reason == "tool_use"


def test_timeout_is_translated_to_provider_timeout_error(monkeypatch):
    _install_fake_boto3(monkeypatch, converse_error=_FakeTimeoutError("timed out"))

    with pytest.raises(ProviderTimeoutError):
        _model().converse([], _TOOLS, timeout=1)


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException", "InternalServerException"]
)
def test_retryable_client_errors_become_transient(monkeypatch, code):
    _install_fake_boto3(monkeypatch, converse_error=_FakeClientError(code))

    with pytest.raises(ProviderTransientError):
        _model().converse([], _TOOLS, timeout=10)


@pytest.mark.parametrize("code", ["AccessDeniedException", "ValidationException", "ResourceNotFoundException"])
def test_non_retryable_client_errors_are_normalized_to_provider_call_error(monkeypatch, code):
    # The regression under test: previously these bubbled out as a raw
    # botocore ClientError and escaped the runtime's provider-error catch.
    _install_fake_boto3(monkeypatch, converse_error=_FakeClientError(code))

    with pytest.raises(ProviderCallError) as excinfo:
        _model().converse([], _TOOLS, timeout=10)
    # The error code (a type/name, not PHI) crosses the boundary; nothing else.
    assert code in str(excinfo.value)


def test_unexpected_response_shape_is_normalized_to_provider_call_error(monkeypatch):
    _install_fake_boto3(monkeypatch, converse_result={"unexpected": "shape"})

    with pytest.raises(ProviderCallError):
        _model().converse([], _TOOLS, timeout=10)


def test_boto3_not_installed_is_normalized_to_provider_not_configured(monkeypatch):
    # w9-fixes P0 4.6: __init__'s own config check can't catch this — a real
    # BEDROCK_MODEL_ID/AWS_REGION can be set while the image simply lacks the
    # SDK, which used to be eligibility-service's own deliberate stance
    # before this PR pinned boto3/botocore there. Kept as defense in depth
    # for any other broken deployment. Setting sys.modules["boto3"] = None is
    # the standard way to make `import boto3` raise ImportError without
    # needing boto3 actually absent from the test environment.
    monkeypatch.setitem(sys.modules, "boto3", None)

    with pytest.raises(ProviderNotConfiguredError):
        _model().converse([], _TOOLS, timeout=10)


# --- converse_stream (w-9-2-planner P1b) -------------------------------------


def test_converse_stream_yields_text_deltas_as_they_arrive(monkeypatch):
    events = [
        {"contentBlockStart": {"start": {}}},
        {"contentBlockDelta": {"delta": {"text": "Cove"}}},
        {"contentBlockDelta": {"delta": {"text": "rage is "}}},
        {"contentBlockDelta": {"delta": {"text": "active."}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    _install_fake_boto3(monkeypatch, stream_events=events)

    stream = list(_model().converse_stream([{"role": "user", "content": [{"text": "hi"}]}], _TOOLS, timeout=10))

    deltas = [e for e in stream if e.kind == "text_delta"]
    assert [e.text for e in deltas] == ["Cove", "rage is ", "active."]
    assert stream[-1].kind == "stop"
    assert stream[-1].stop_reason == "end_turn"


def test_converse_stream_assembles_a_tool_calls_input_from_fragments(monkeypatch):
    events = [
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": "get_coverage_on_file"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "{\"a"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "\": 1}"}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]
    _install_fake_boto3(monkeypatch, stream_events=events)

    stream = list(_model().converse_stream([], _TOOLS, timeout=10))

    tool_events = [e for e in stream if e.kind == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_call.id == "t1"
    assert tool_events[0].tool_call.name == "get_coverage_on_file"
    assert tool_events[0].tool_call.arguments == {"a": 1}
    # The assembled tool call is a distinct event kind — never mistaken for
    # user-facing text by a caller that only forwards "text_delta" events.
    assert all(e.kind != "text_delta" for e in tool_events)


def test_converse_stream_never_emits_a_text_delta_for_tool_use_input_fragments(monkeypatch):
    # The exact leak this design must prevent: raw tool-argument JSON
    # fragments must never be mistaken for answer text and forwarded to the
    # browser mid-assembly.
    events = [
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": "verify_current_eligibility"}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]
    _install_fake_boto3(monkeypatch, stream_events=events)

    stream = list(_model().converse_stream([], _TOOLS, timeout=10))

    assert all(e.kind != "text_delta" for e in stream)


def test_converse_stream_open_timeout_is_translated_to_provider_timeout_error(monkeypatch):
    _install_fake_boto3(monkeypatch, stream_open_error=_FakeTimeoutError("timed out"))

    with pytest.raises(ProviderTimeoutError):
        list(_model().converse_stream([], _TOOLS, timeout=1))


def test_converse_stream_mid_stream_failure_raises_rather_than_truncating_silently(monkeypatch):
    # A stream that yields some real text and then fails must still raise —
    # the runtime's own catch is what turns this into one sanitized error
    # event; this port must never swallow it and let the partial text look
    # like a complete answer.
    events = [{"contentBlockDelta": {"delta": {"text": "Partial"}}}]
    _install_fake_boto3(monkeypatch, stream_events=events, stream_iter_error=_FakeClientError("InternalServerException"))

    collected = []
    with pytest.raises(ProviderTransientError):
        for event in _model().converse_stream([], _TOOLS, timeout=10):
            collected.append(event)
    assert [e.text for e in collected if e.kind == "text_delta"] == ["Partial"]


@pytest.mark.parametrize(
    "event_key,expected_error",
    [
        ("throttlingException", ProviderTransientError),
        ("serviceUnavailableException", ProviderTransientError),
        ("internalServerException", ProviderTransientError),
        ("modelStreamErrorException", ProviderTransientError),
        ("validationException", ProviderCallError),
    ],
)
def test_converse_stream_in_band_error_events_raise_rather_than_end_silently(monkeypatch, event_key, expected_error):
    # w-9-2-planner P1b review fix (CS-ERROR-EVENTS): Bedrock delivers a
    # mid-stream failure as one of these top-level event keys, not as a
    # raised ClientError — the SDK iterator itself keeps going normally. A
    # loop that only recognizes contentBlock*/messageStop must not treat
    # this as a clean (if truncated) completion.
    events = [
        {"contentBlockDelta": {"delta": {"text": "Partial"}}},
        {event_key: {"message": "provider trouble"}},
    ]
    _install_fake_boto3(monkeypatch, stream_events=events)

    collected = []
    with pytest.raises(expected_error):
        for event in _model().converse_stream([], _TOOLS, timeout=10):
            collected.append(event)
    assert [e.text for e in collected if e.kind == "text_delta"] == ["Partial"]
    assert all(e.kind != "stop" for e in collected)


def test_converse_stream_ending_without_a_stop_event_raises_provider_call_error(monkeypatch):
    # Defense in depth: a stream that exhausts without ever delivering
    # messageStop or a recognized error event must not be treated as a
    # normal, complete answer.
    events = [{"contentBlockDelta": {"delta": {"text": "Partial"}}}]
    _install_fake_boto3(monkeypatch, stream_events=events)

    collected = []
    with pytest.raises(ProviderCallError):
        for event in _model().converse_stream([], _TOOLS, timeout=10):
            collected.append(event)
    assert [e.text for e in collected if e.kind == "text_delta"] == ["Partial"]


def test_converse_stream_closes_the_underlying_event_stream_when_fully_consumed(monkeypatch):
    events = [{"contentBlockDelta": {"delta": {"text": "hi"}}}, {"messageStop": {"stopReason": "end_turn"}}]
    fake_stream = _install_fake_boto3(monkeypatch, stream_events=events)

    list(_model().converse_stream([], _TOOLS, timeout=10))

    assert fake_stream.closed is True


def test_converse_stream_closes_the_underlying_event_stream_on_early_exit(monkeypatch):
    # Simulates a client disconnect: the caller stops iterating before the
    # stream ends. The generator's `finally` must still close the
    # underlying provider stream rather than leaking it.
    events = [
        {"contentBlockDelta": {"delta": {"text": "one"}}},
        {"contentBlockDelta": {"delta": {"text": "two"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    fake_stream = _install_fake_boto3(monkeypatch, stream_events=events)

    gen = _model().converse_stream([], _TOOLS, timeout=10)
    next(gen)  # take only the first event
    gen.close()

    assert fake_stream.closed is True


# --- provider-call metric: exactly one outcome per stream (review finding
# AI-PROVIDER-STREAM-CLOSE-MISSING) ------------------------------------------


def _stream_call_count(*, outcome: str) -> float:
    return ai_metrics.BEDROCK_PROVIDER_CALLS.labels(
        provider="bedrock", model="anthropic.claude-3-5-sonnet",
        use_case="eligibility_agent_chat", operation="converse_stream", outcome=outcome,
    )._value.get()


def test_an_early_close_records_exactly_one_cancelled_outcome(monkeypatch):
    # The ordinary case for a live chat stream: the browser client
    # disconnects, or the caller simply stops reading. Python delivers this
    # as GeneratorExit at the suspended `yield` — a BaseException a plain
    # `except Exception:` never sees, which is exactly how this call used to
    # go unrecorded.
    events = [
        {"contentBlockDelta": {"delta": {"text": "one"}}},
        {"contentBlockDelta": {"delta": {"text": "two"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    _install_fake_boto3(monkeypatch, stream_events=events)
    before = _stream_call_count(outcome="cancelled")
    before_success = _stream_call_count(outcome="success")
    before_error = _stream_call_count(outcome="provider_error")

    gen = _model().converse_stream([], _TOOLS, timeout=10)
    next(gen)  # start the stream, consume one event
    gen.close()

    assert _stream_call_count(outcome="cancelled") == before + 1
    assert _stream_call_count(outcome="success") == before_success, "not also counted as success"
    assert _stream_call_count(outcome="provider_error") == before_error, "not also counted as an error"


def test_a_fully_consumed_stream_records_exactly_one_success_outcome(monkeypatch):
    events = [{"contentBlockDelta": {"delta": {"text": "hi"}}}, {"messageStop": {"stopReason": "end_turn"}}]
    _install_fake_boto3(monkeypatch, stream_events=events)
    before = _stream_call_count(outcome="success")

    list(_model().converse_stream([], _TOOLS, timeout=10))

    assert _stream_call_count(outcome="success") == before + 1


def test_a_provider_failure_mid_stream_records_exactly_one_provider_error_outcome(monkeypatch):
    _install_fake_boto3(
        monkeypatch,
        stream_events=[{"contentBlockDelta": {"delta": {"text": "Partial"}}}],
        stream_iter_error=_FakeTimeoutError(),
    )
    before = _stream_call_count(outcome="provider_error")

    with pytest.raises(ProviderTimeoutError):
        for _ in _model().converse_stream([], _TOOLS, timeout=10):
            pass

    assert _stream_call_count(outcome="provider_error") == before + 1


def test_a_generator_exit_still_propagates_and_still_closes_the_stream(monkeypatch):
    # The metric change must not touch the close-protocol or resource-close
    # behavior already proven by
    # test_converse_stream_closes_the_underlying_event_stream_on_early_exit.
    events = [
        {"contentBlockDelta": {"delta": {"text": "one"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    fake_stream = _install_fake_boto3(monkeypatch, stream_events=events)

    gen = _model().converse_stream([], _TOOLS, timeout=10)
    next(gen)
    gen.close()  # would raise RuntimeError if GeneratorExit were suppressed and the generator kept yielding

    assert fake_stream.closed is True


def test_default_converse_stream_fallback_replays_a_blocking_converse_as_one_chunk():
    # ToolCapableModel's own default (used by any adapter that doesn't
    # override it, e.g. Ollama) — an honest, non-token-level degrade, not a
    # broken one.
    from libs.eligibility_agent.bedrock_tool_port import ConverseTurn, ToolCall, ToolCapableModel

    class _Blocking(ToolCapableModel):
        def converse(self, messages, tools, *, timeout):
            return ConverseTurn(text="whole answer", tool_calls=[ToolCall(id="t1", name="x", arguments={})])

    events = list(_Blocking().converse_stream([], _TOOLS, timeout=10))

    assert [e.kind for e in events] == ["text_delta", "tool_call", "stop"]
    assert events[0].text == "whole answer"
    assert events[1].tool_call.id == "t1"
