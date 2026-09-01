"""A tool-capable Bedrock Converse port — separate from libs/llm_client's
completion-only Provider interface. Provider.complete() takes a prompt and
returns text; it has no notion of tools, and every existing caller (e.g.
libs/rag_eval, anything using LLMClient) relies on that being simple and
stable. Bolting tool-calling onto it would distort a general-purpose
completion client to serve one agent-specific use case. This port exists
instead, purpose-built for a tool-calling loop, and libs/llm_client is left
untouched.

boto3 is imported lazily (inside converse()), exactly like
libs/llm_client/providers/bedrock_provider.py, so nothing that merely imports
this module — or this whole package — requires it installed. Error
translation reuses libs.llm_client.errors' existing ProviderTimeoutError/
ProviderTransientError/ProviderNotConfiguredError vocabulary rather than
inventing a parallel one, since it's the same retryable-vs-not distinction
for the same underlying AWS SDK, just behind a different method shape.
"""
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from libs.metrics import ai as ai_metrics
from libs.llm_client.errors import (
    ProviderCallError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    ProviderTransientError,
)

_RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "ModelTimeoutException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
    "InternalServerException",
}

# w-9-2-planner P1b review fix (CS-ERROR-EVENTS): ConverseStream delivers a
# mid-stream provider failure as one of these top-level event keys, not as a
# raised ClientError — the SDK iterator itself keeps yielding normally, so a
# loop that only recognizes contentBlockStart/Delta/Stop and messageStop
# silently treats the failure as if the turn simply ended. Each maps to the
# same timeout/transient/call-error vocabulary converse() already uses.
_STREAM_ERROR_EVENT_TYPES = {
    "throttlingException": ProviderTransientError,
    "serviceUnavailableException": ProviderTransientError,
    "internalServerException": ProviderTransientError,
    "modelStreamErrorException": ProviderTransientError,
    "validationException": ProviderCallError,
}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ConverseTurn:
    text: Optional[str]
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop_reason: str = ""


@dataclass(frozen=True)
class ConverseStreamEvent:
    """One increment of a streamed turn — w-9-2-planner P1b. `kind` is
    "text_delta" (a piece of user-facing answer text, safe to forward to the
    browser as it arrives), "tool_call" (a fully-assembled tool call, never
    forwarded to the browser — internal dispatch only), or "stop" (the turn
    ended; carries Bedrock's own stop_reason, itself just a short enum
    string, not raw provider output)."""

    kind: str
    text: Optional[str] = None
    tool_call: Optional[ToolCall] = None
    stop_reason: str = ""


class ToolCapableModel(ABC):
    """A single-turn, tool-capable model call — the seam that makes the
    raw_bedrock AgentRuntime swappable/testable, mirroring how
    libs/llm_client/providers/base.py's Provider does for LLMClient."""

    @abstractmethod
    def converse(self, messages: list, tools: list, *, timeout: float) -> ConverseTurn:
        raise NotImplementedError

    def converse_stream(self, messages: list, tools: list, *, timeout: float) -> Iterator[ConverseStreamEvent]:
        """Default, non-streaming fallback: a single blocking converse()
        call, replayed as one text_delta (if any) + any tool_calls + a stop
        event. Real incremental delivery is only implemented for
        BedrockConverseToolModel below (this repo's actual default runtime);
        every other ToolCapableModel adapter (Ollama, test doubles) still
        WORKS through this fallback, just without token-level granularity —
        an honest degrade, not a broken one."""
        turn = self.converse(messages, tools, timeout=timeout)
        if turn.text:
            yield ConverseStreamEvent(kind="text_delta", text=turn.text)
        for call in turn.tool_calls:
            yield ConverseStreamEvent(kind="tool_call", tool_call=call)
        yield ConverseStreamEvent(kind="stop", stop_reason=turn.stop_reason)


# Bounded metrics label for the eligibility assistant surface.
_METRICS_USE_CASE = "eligibility_agent_chat"


class BedrockConverseToolModel(ToolCapableModel):
    def __init__(self, model_id: str = None, region: str = None):
        self._model_id = model_id or os.getenv("BEDROCK_MODEL_ID")
        self._region = region or os.getenv("AWS_REGION")
        if not self._model_id or self._model_id == "changeme":
            raise ProviderNotConfiguredError("BEDROCK_MODEL_ID is not configured")
        if not self._region:
            raise ProviderNotConfiguredError("AWS_REGION is not configured")

    def converse(self, messages: list, tools: list, *, timeout: float) -> ConverseTurn:
        """Timed, counted wrapper around the real Converse call.

        This surface reports NO token usage — Bedrock returns a usage block
        but `_converse` discards it, so only the call, its categorical
        outcome and its duration are measured. Recording a zero here would
        invent a measurement this port never made.
        """
        started = time.monotonic()
        try:
            turn = self._converse(messages, tools, timeout=timeout)
        except Exception:
            # Categorical outcome only: every error type this raises is
            # normalized elsewhere, and none of them belongs in a label.
            ai_metrics.record_provider_call(
                use_case=_METRICS_USE_CASE, model_id=self._model_id, operation="converse",
                outcome="provider_error", duration_seconds=time.monotonic() - started,
            )
            raise
        ai_metrics.record_provider_call(
            use_case=_METRICS_USE_CASE, model_id=self._model_id, operation="converse",
            outcome="success", duration_seconds=time.monotonic() - started,
        )
        return turn

    def _converse(self, messages: list, tools: list, *, timeout: float) -> ConverseTurn:
        try:
            import boto3  # lazy import — see module docstring
            from botocore.config import Config
            from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError
        except ImportError as exc:
            # w9-fixes P0 4.6: a deployment can set a real BEDROCK_MODEL_ID
            # (so __init__ above never raises ProviderNotConfiguredError)
            # while the image simply doesn't have boto3 installed — this
            # repo's own eligibility-service used to leave boto3 unpinned on
            # exactly that theory (see its requirements.txt, now pinned).
            # Before this, a missing SDK raised a bare ModuleNotFoundError
            # here, which isn't an LLMClientError and escaped
            # RawBedrockAgentRuntime.handle_message's provider-error catch
            # as an unhandled 500. A missing SDK is a configuration problem,
            # same as a missing model id/region above — this stays as
            # defense in depth for any other broken deployment.
            raise ProviderNotConfiguredError(type(exc).__name__) from exc

        client = boto3.client(
            "bedrock-runtime",
            region_name=self._region,
            config=Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 0}),
        )
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t["description"],
                        "inputSchema": {"json": t["input_schema"]},
                    }
                }
                for t in tools
            ]
        }
        try:
            response = client.converse(modelId=self._model_id, messages=messages, toolConfig=tool_config)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise ProviderTimeoutError(type(exc).__name__) from exc
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in _RETRYABLE_ERROR_CODES:
                raise ProviderTransientError(type(exc).__name__) from exc
            # Non-retryable (AccessDenied, ValidationException, ...). Unlike the
            # completion-only bedrock_provider (which re-raises so a developer
            # sees the misconfiguration), this agent-facing port normalizes it
            # to ProviderCallError so the runtime can degrade gracefully to a
            # safe reply rather than letting a raw SDK exception escape
            # handle_message. Only the error TYPE crosses the boundary.
            raise ProviderCallError(error_code or type(exc).__name__) from exc

        try:
            content = response["output"]["message"]["content"]
            text_parts = [block["text"] for block in content if "text" in block]
            tool_calls = [
                ToolCall(
                    id=block["toolUse"]["toolUseId"],
                    name=block["toolUse"]["name"],
                    arguments=block["toolUse"].get("input") or {},
                )
                for block in content
                if "toolUse" in block
            ]
        except (KeyError, TypeError, IndexError) as exc:
            # An unexpected Converse response shape (a model/SDK change) must
            # not throw a raw KeyError out of the agent — surface it as a
            # controlled provider failure, TYPE only.
            raise ProviderCallError(type(exc).__name__) from exc

        return ConverseTurn(
            text="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=response.get("stopReason", ""),
        )

    def converse_stream(self, messages: list, tools: list, *, timeout: float) -> Iterator[ConverseStreamEvent]:
        """Timed, counted wrapper around the real streaming Converse call.

        Duration here is the WHOLE stream, first byte to last — the natural
        analogue of the blocking call's round trip. The outcome is only known
        once the generator finishes or raises, so it is recorded then rather
        than on the first yield. Like the blocking path, no tokens: this port
        discards the provider's usage block.
        """
        started = time.monotonic()
        try:
            for event in self._converse_stream(messages, tools, timeout=timeout):
                yield event
        except Exception:
            ai_metrics.record_provider_call(
                use_case=_METRICS_USE_CASE, model_id=self._model_id, operation="converse_stream",
                outcome="provider_error", duration_seconds=time.monotonic() - started,
            )
            raise
        ai_metrics.record_provider_call(
            use_case=_METRICS_USE_CASE, model_id=self._model_id, operation="converse_stream",
            outcome="success", duration_seconds=time.monotonic() - started,
        )

    def _converse_stream(self, messages: list, tools: list, *, timeout: float) -> Iterator[ConverseStreamEvent]:
        """w-9-2-planner P1b: Bedrock's ConverseStream API, translated to
        ConverseStreamEvent. Text deltas are yielded AS THEY ARRIVE — the
        caller (RawBedrockAgentRuntime) forwards exactly these to the
        browser, live. A toolUse content block's `input` streams as
        fragments of a JSON string across several deltas — accumulated
        silently here and parsed once as a whole on contentBlockStop; a
        tool call is never itself forwarded to the browser, only dispatched
        internally by the runtime.

        Error translation mirrors converse() above: the same timeout/
        transient/call-error vocabulary, whether raised before the stream
        opens or partway through iterating it (a stream that fails mid-turn
        after already yielding some text still ends in a raised
        LLMClientError here, never a silent truncation reported as
        complete — the runtime's own try/except around this generator is
        what turns that into one sanitized terminal error event)."""
        try:
            import boto3  # lazy import — see module docstring
            from botocore.config import Config
            from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError
        except ImportError as exc:
            raise ProviderNotConfiguredError(type(exc).__name__) from exc

        client = boto3.client(
            "bedrock-runtime",
            region_name=self._region,
            config=Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 0}),
        )
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t["description"],
                        "inputSchema": {"json": t["input_schema"]},
                    }
                }
                for t in tools
            ]
        }
        try:
            response = client.converse_stream(modelId=self._model_id, messages=messages, toolConfig=tool_config)
            event_stream = response["stream"]
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise ProviderTimeoutError(type(exc).__name__) from exc
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in _RETRYABLE_ERROR_CODES:
                raise ProviderTransientError(type(exc).__name__) from exc
            raise ProviderCallError(error_code or type(exc).__name__) from exc

        pending_tool_use: Optional[dict] = None  # {"id", "name", "input_json": [fragments]}
        saw_stop = False
        try:
            for event in event_stream:
                error_key = next((k for k in _STREAM_ERROR_EVENT_TYPES if k in event), None)
                if error_key is not None:
                    # An in-band failure event, not a raised ClientError — the
                    # SDK iterator would otherwise just end normally right
                    # here, reading as a clean (but truncated) completion.
                    raise _STREAM_ERROR_EVENT_TYPES[error_key](error_key)
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"].get("start", {})
                    if "toolUse" in start:
                        pending_tool_use = {
                            "id": start["toolUse"]["toolUseId"],
                            "name": start["toolUse"]["name"],
                            "input_json": [],
                        }
                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    if "text" in delta:
                        yield ConverseStreamEvent(kind="text_delta", text=delta["text"])
                    elif "toolUse" in delta and pending_tool_use is not None:
                        pending_tool_use["input_json"].append(delta["toolUse"].get("input", ""))
                elif "contentBlockStop" in event:
                    if pending_tool_use is not None:
                        raw = "".join(pending_tool_use["input_json"])
                        try:
                            arguments = json.loads(raw) if raw else {}
                        except (ValueError, TypeError):
                            arguments = {}
                        yield ConverseStreamEvent(
                            kind="tool_call",
                            tool_call=ToolCall(
                                id=pending_tool_use["id"], name=pending_tool_use["name"], arguments=arguments
                            ),
                        )
                        pending_tool_use = None
                elif "messageStop" in event:
                    saw_stop = True
                    yield ConverseStreamEvent(kind="stop", stop_reason=event["messageStop"].get("stopReason", ""))
            if not saw_stop:
                # The iterator exhausted without ever delivering messageStop
                # or a recognized error event — a truncated stream must not
                # be treated as a normal completion (see CS-ERROR-EVENTS).
                raise ProviderCallError("StreamEndedWithoutStop")
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise ProviderTimeoutError(type(exc).__name__) from exc
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in _RETRYABLE_ERROR_CODES:
                raise ProviderTransientError(type(exc).__name__) from exc
            raise ProviderCallError(error_code or type(exc).__name__) from exc
        finally:
            close = getattr(event_stream, "close", None)
            if callable(close):
                close()
