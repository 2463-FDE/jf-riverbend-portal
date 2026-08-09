"""A tool-capable Ollama chat adapter — Stage 2 (feature-readiness)'s local
demo alternative to `bedrock_tool_port.BedrockConverseToolModel`.

Implements the same `ToolCapableModel` port
(`converse(messages, tools, *, timeout) -> ConverseTurn`), so it plugs
straight into `RawBedrockAgentRuntime`'s existing bounded loop, tool
allowlist/dispatch, and structured-memory persistence unchanged — this is
ONLY a model adapter, not a new runtime (see `runtime.py::build_agent_runtime`,
which selects this for `ELIGIBILITY_AGENT_RUNTIME=ollama`).

`RawBedrockAgentRuntime` speaks Bedrock's Converse message shape
(`{"role": ..., "content": [{"text": ...} | {"toolUse": {...}} | {"toolResult": {...}}]}`).
Ollama's `/api/chat` tool-calling format is OpenAI-style instead
(`{"role": "assistant", "content": "...", "tool_calls": [...]}` /
`{"role": "tool", "tool_call_id": ..., "content": "..."}`). `_to_ollama_messages`/
`_to_ollama_tools` translate one direction; the response is translated back
into a `ConverseTurn` the same way `BedrockConverseToolModel` does. Verified
live against a local `llama3.2:3b` (see docs/runbook.md's Stage 2 setup
section) — Ollama's response DOES include a `tool_calls[].id`, used directly
rather than invented, with a fallback only for a future/other model that
omits it.

Like `OllamaProvider` (libs/llm_client/providers/ollama_provider.py), the
base URL and model name come from config, never hardcoded, and no network
call happens unless this adapter is actually selected. `transport` mirrors
`eligibility_tool.CheckEligibilityTool`'s own constructor param — tests
inject an `httpx.MockTransport`, never a live server.
"""
import json
import os
from typing import Optional

import httpx

from libs.llm_client.errors import (
    ProviderCallError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    ProviderTransientError,
)

from .bedrock_tool_port import ConverseTurn, ToolCall, ToolCapableModel


class OllamaToolCapableModel(ToolCapableModel):
    def __init__(
        self,
        base_url: str = None,
        model: str = None,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self._base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "")).rstrip("/")
        self._model = model or os.getenv("OLLAMA_MODEL")
        if not self._base_url:
            raise ProviderNotConfiguredError("OLLAMA_BASE_URL is not configured")
        if not self._model or self._model == "changeme":
            raise ProviderNotConfiguredError("OLLAMA_MODEL is not configured")
        self._transport = transport

    def converse(self, messages: list, tools: list, *, timeout: float) -> ConverseTurn:
        try:
            with httpx.Client(transport=self._transport, timeout=timeout) as client:
                response = client.post(
                    f"{self._base_url}/api/chat",
                    json={
                        "model": self._model,
                        "messages": _to_ollama_messages(messages),
                        "tools": _to_ollama_tools(tools),
                        "stream": False,
                    },
                )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(type(exc).__name__) from exc
        except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
            raise ProviderTransientError(type(exc).__name__) from exc

        try:
            body = response.json()
            message = body["message"]
            tool_calls = [
                ToolCall(
                    id=call.get("id") or f"call_{i}",
                    name=call["function"]["name"],
                    arguments=call["function"].get("arguments") or {},
                )
                for i, call in enumerate(message.get("tool_calls") or [])
            ]
        except (KeyError, TypeError, ValueError) as exc:
            # An unexpected /api/chat response shape must not throw a raw
            # KeyError out of the agent — same convention as
            # BedrockConverseToolModel.converse for the same reason.
            raise ProviderCallError(type(exc).__name__) from exc

        return ConverseTurn(
            text=message.get("content") or None,
            tool_calls=tool_calls,
            stop_reason=body.get("done_reason", ""),
        )


def _to_ollama_messages(messages: list) -> list:
    """Bedrock Converse shape -> Ollama chat shape. See module docstring."""
    ollama_messages = []
    for msg in messages:
        blocks = msg.get("content") or []
        tool_result_blocks = [b["toolResult"] for b in blocks if "toolResult" in b]
        if tool_result_blocks:
            # Bedrock wraps a tool's result as a "user" turn; Ollama wants
            # each result as its own "tool" message instead.
            for result in tool_result_blocks:
                payload = next((c["json"] for c in result.get("content", []) if "json" in c), {})
                ollama_messages.append(
                    {"role": "tool", "tool_call_id": result["toolUseId"], "content": json.dumps(payload)}
                )
            continue

        text = "".join(b["text"] for b in blocks if "text" in b)
        tool_use_blocks = [b["toolUse"] for b in blocks if "toolUse" in b]
        entry = {"role": msg["role"], "content": text}
        if tool_use_blocks:
            entry["tool_calls"] = [
                {"id": tu["toolUseId"], "function": {"name": tu["name"], "arguments": tu.get("input") or {}}}
                for tu in tool_use_blocks
            ]
        ollama_messages.append(entry)
    return ollama_messages


def _to_ollama_tools(tools: list) -> list:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in tools
    ]
