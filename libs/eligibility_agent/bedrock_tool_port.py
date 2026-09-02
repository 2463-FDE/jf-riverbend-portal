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

from libs.agent_budget import BUDGETS
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
    # W10 Metrics Stage 4: the provider's own reported usage for this exact
    # call, when the response carried one — None means "not reported",
    # never "zero".
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass(frozen=True)
class ConverseStreamEvent:
    """One increment of a streamed turn — w-9-2-planner P1b. `kind` is
    "text_delta" (a piece of model answer text — NOT automatically safe to
    forward: a turn can emit prose before the tool call that decides whether
    that prose is true, so the caller buffers per turn and decides once the
    turn is complete; see runtimes/raw_bedrock.py), "tool_call" (a
    fully-assembled tool call, never
    forwarded to the browser — internal dispatch only), "stop" (the turn
    ended; carries Bedrock's own stop_reason, itself just a short enum
    string, not raw provider output), or "usage" (W10 Metrics Stage 4:
    Bedrock's ConverseStream reports token usage in a trailing `metadata`
    event that arrives AFTER "stop", never combined with it — never forwarded
    to the browser, internal accounting only)."""

    kind: str
    text: Optional[str] = None
    tool_call: Optional[ToolCall] = None
    stop_reason: str = ""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


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
        # W10 Metrics Stage 4: centrally bounded, not a locally-chosen
        # number — see libs/agent_budget's "maximum output tokens" bound.
        # Bedrock's Converse API has no bound at all unless one is supplied,
        # so before this the eligibility loop's per-turn output was entirely
        # provider-default, unbounded from this side.
        self._max_output_tokens = BUDGETS[_METRICS_USE_CASE].max_output_tokens

    @property
    def model_id(self) -> Optional[str]:
        return self._model_id

    def converse(self, messages: list, tools: list, *, timeout: float) -> ConverseTurn:
        """Timed, counted wrapper around the real Converse call.

        W10 Metrics Stage 4: now records real token usage when Bedrock
        reports one (previously this surface always discarded it — see
        `_converse`'s own docstring update).
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
            input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
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
            response = client.converse(
                modelId=self._model_id, messages=messages, toolConfig=tool_config,
                inferenceConfig={"maxTokens": self._max_output_tokens},
            )
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

        usage = response.get("usage") or {}
        return ConverseTurn(
            text="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=response.get("stopReason", ""),
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
        )

    def converse_stream(self, messages: list, tools: list, *, timeout: float) -> Iterator[ConverseStreamEvent]:
        """Timed, counted wrapper around the real streaming Converse call.

        Duration here is the WHOLE stream, first byte to last — the natural
        analogue of the blocking call's round trip. W10 Metrics Stage 4: now
        records real token usage from the trailing "usage" event (see
        `_converse_stream`'s docstring) when the stream completed normally —
        a cancelled or errored stream may never reach that event, so usage
        stays whatever was last observed (None if the stream never got that
        far), never invented.

        Exactly one outcome is recorded, in the `finally`, once the stream has
        started — review finding AI-PROVIDER-STREAM-CLOSE-MISSING: a plain
        `except Exception` never sees `GeneratorExit` (it is a BaseException),
        so a caller that stops iterating early — a client disconnect, the
        ordinary case for a live chat stream — used to leave this call
        unrecorded. `GeneratorExit` is caught here ONLY to classify the
        outcome as `cancelled`; it is always re-raised unchanged, never
        suppressed, so the generator-close protocol is unaffected. Closing
        the underlying Bedrock event stream is `_converse_stream`'s own
        `finally` (below), untouched by this change.
        """
        started = time.monotonic()
        outcome = "success"
        input_tokens = output_tokens = None
        try:
            for event in self._converse_stream(messages, tools, timeout=timeout):
                if event.kind == "usage":
                    input_tokens, output_tokens = event.input_tokens, event.output_tokens
                yield event  # the caller (RawBedrockAgentRuntime) also reads "usage" for durable accounting
        except GeneratorExit:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "provider_error"
            raise
        finally:
            ai_metrics.record_provider_call(
                use_case=_METRICS_USE_CASE, model_id=self._model_id, operation="converse_stream",
                outcome=outcome, duration_seconds=time.monotonic() - started,
                input_tokens=input_tokens, output_tokens=output_tokens,
            )

    def _converse_stream(self, messages: list, tools: list, *, timeout: float) -> Iterator[ConverseStreamEvent]:
        """w-9-2-planner P1b: Bedrock's ConverseStream API, translated to
        ConverseStreamEvent. Text deltas are yielded here AS THEY ARRIVE,
        but that is a transport detail, not permission to display them: the
        caller (RawBedrockAgentRuntime) buffers them for the whole turn and
        decides what to release once it knows whether the turn also called a
        tool. A toolUse content block's `input` streams as
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
            response = client.converse_stream(
                modelId=self._model_id, messages=messages, toolConfig=tool_config,
                inferenceConfig={"maxTokens": self._max_output_tokens},
            )
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
                elif "metadata" in event:
                    # W10 Metrics Stage 4: ConverseStream's usage block
                    # arrives in a trailing "metadata" event, AFTER
                    # "messageStop" — never combined with it, so this is a
                    # separate event kind rather than a field the "stop"
                    # event could have carried at the time it was yielded.
                    usage = event["metadata"].get("usage") or {}
                    yield ConverseStreamEvent(
                        kind="usage", input_tokens=usage.get("inputTokens"), output_tokens=usage.get("outputTokens"),
                    )
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
