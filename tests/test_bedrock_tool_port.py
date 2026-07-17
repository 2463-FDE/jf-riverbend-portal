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


def _install_fake_boto3(monkeypatch, *, converse_result=None, converse_error=None):
    class _FakeBedrockClient:
        def converse(self, **kwargs):
            if converse_error is not None:
                raise converse_error
            return converse_result

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
