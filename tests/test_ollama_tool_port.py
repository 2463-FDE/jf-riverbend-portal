"""Tests for the tool-capable Ollama chat adapter
(libs/eligibility_agent/ollama_tool_port.py) — Stage 2 (feature-readiness).

All calls go through httpx.MockTransport; no network, no live Ollama server.
Response fixtures below mirror the actual /api/chat shape verified live
against a local llama3.2:3b (see the module docstring and docs/runbook.md's
Stage 2 setup section) — not a guessed/idealized shape.
"""
import json

import httpx
import pytest

from libs.eligibility_agent.bedrock_tool_port import ConverseTurn
from libs.eligibility_agent.eligibility_tool import VERIFY_TOOL_SPEC as TOOL_SPEC
from libs.eligibility_agent.ollama_tool_port import (
    OllamaToolCapableModel,
    _to_ollama_messages,
    _to_ollama_tools,
)
from libs.llm_client.errors import (
    ProviderCallError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    ProviderTransientError,
)


def _model(transport):
    return OllamaToolCapableModel(base_url="http://localhost:11434", model="llama3.2:3b", transport=transport)


# --- construction: fails closed exactly like OllamaProvider -----------------


def test_missing_base_url_raises_not_configured():
    with pytest.raises(ProviderNotConfiguredError):
        OllamaToolCapableModel(base_url="", model="llama3.2:3b")


def test_missing_model_raises_not_configured():
    with pytest.raises(ProviderNotConfiguredError):
        OllamaToolCapableModel(base_url="http://localhost:11434", model=None)


def test_placeholder_model_raises_not_configured():
    with pytest.raises(ProviderNotConfiguredError):
        OllamaToolCapableModel(base_url="http://localhost:11434", model="changeme")


# --- converse(): response translation, verified against a real recorded shape

# Recorded live from `curl http://localhost:11434/api/chat` against
# llama3.2:3b with the check_eligibility tool offered (see module docstring).
_LIVE_TOOL_CALL_RESPONSE = {
    "model": "llama3.2:3b",
    "message": {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_7qhntano", "function": {"index": 0, "name": "check_eligibility", "arguments": {}}}],
    },
    "done": True,
    "done_reason": "stop",
}

_LIVE_TEXT_RESPONSE = {
    "model": "llama3.2:3b",
    "message": {
        "role": "assistant",
        "content": "Based on the tool call response, your coverage is currently active.",
    },
    "done": True,
    "done_reason": "stop",
}


def test_tool_call_response_is_translated_to_a_tool_call():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_LIVE_TOOL_CALL_RESPONSE))
    model = _model(transport)

    turn = model.converse([{"role": "user", "content": [{"text": "check my eligibility"}]}], [TOOL_SPEC], timeout=5.0)

    assert turn.text is None
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].id == "call_7qhntano"
    assert turn.tool_calls[0].name == "check_eligibility"
    assert turn.tool_calls[0].arguments == {}
    assert turn.stop_reason == "stop"


def test_text_only_response_has_no_tool_calls():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_LIVE_TEXT_RESPONSE))
    model = _model(transport)

    turn = model.converse([{"role": "user", "content": [{"text": "hi"}]}], [TOOL_SPEC], timeout=5.0)

    assert turn.tool_calls == []
    assert "active" in turn.text


def test_missing_tool_call_id_falls_back_to_a_generated_one():
    # Defensive: a future/other Ollama-compatible model might not supply an
    # id the way llama3.2:3b does — must not crash on a missing key.
    response = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "check_eligibility", "arguments": {}}}],
        },
        "done_reason": "stop",
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    model = _model(transport)

    turn = model.converse([{"role": "user", "content": [{"text": "hi"}]}], [TOOL_SPEC], timeout=5.0)

    assert turn.tool_calls[0].id == "call_0"


# --- converse(): request translation ----------------------------------------


def test_request_sends_the_translated_tool_spec_and_model():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_LIVE_TEXT_RESPONSE)

    model = _model(httpx.MockTransport(handler))
    model.converse([{"role": "user", "content": [{"text": "hi"}]}], [TOOL_SPEC], timeout=5.0)

    assert captured["body"]["model"] == "llama3.2:3b"
    assert captured["body"]["stream"] is False
    assert captured["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": TOOL_SPEC["name"],
                "description": TOOL_SPEC["description"],
                "parameters": TOOL_SPEC["input_schema"],
            },
        }
    ]
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


# --- error boundary: same provider-error vocabulary as BedrockConverseToolModel


def test_timeout_raises_provider_timeout_error():
    def handler(request):
        raise httpx.TimeoutException("timed out")

    model = _model(httpx.MockTransport(handler))

    with pytest.raises(ProviderTimeoutError):
        model.converse([{"role": "user", "content": [{"text": "hi"}]}], [TOOL_SPEC], timeout=1.0)


def test_connection_error_raises_provider_transient_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    model = _model(httpx.MockTransport(handler))

    with pytest.raises(ProviderTransientError):
        model.converse([{"role": "user", "content": [{"text": "hi"}]}], [TOOL_SPEC], timeout=1.0)


def test_http_error_status_raises_provider_transient_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json={"error": "internal"}))
    model = _model(transport)

    with pytest.raises(ProviderTransientError):
        model.converse([{"role": "user", "content": [{"text": "hi"}]}], [TOOL_SPEC], timeout=1.0)


def test_malformed_response_shape_raises_provider_call_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"no_message_key": True}))
    model = _model(transport)

    with pytest.raises(ProviderCallError):
        model.converse([{"role": "user", "content": [{"text": "hi"}]}], [TOOL_SPEC], timeout=1.0)


# --- Bedrock <-> Ollama message translation, exercised directly -------------


def test_assistant_tool_use_block_becomes_a_tool_call_message():
    bedrock_messages = [
        {"role": "user", "content": [{"text": "check eligibility"}]},
        {
            "role": "assistant",
            "content": [{"toolUse": {"toolUseId": "call_1", "name": "check_eligibility", "input": {}}}],
        },
        {
            "role": "user",
            "content": [{"toolResult": {"toolUseId": "call_1", "content": [{"json": {"status": "active"}}]}}],
        },
    ]

    ollama_messages = _to_ollama_messages(bedrock_messages)

    assert ollama_messages[0] == {"role": "user", "content": "check eligibility"}
    assert ollama_messages[1] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_1", "function": {"name": "check_eligibility", "arguments": {}}}],
    }
    assert ollama_messages[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": json.dumps({"status": "active"}),
    }


def test_tool_spec_translation_matches_openai_style_function_shape():
    translated = _to_ollama_tools([TOOL_SPEC])

    assert translated == [
        {
            "type": "function",
            "function": {
                "name": TOOL_SPEC["name"],
                "description": TOOL_SPEC["description"],
                "parameters": TOOL_SPEC["input_schema"],
            },
        }
    ]
