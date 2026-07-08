"""Tests for the Bedrock adapter (libs/llm_client/providers/bedrock_provider.py).

`boto3`/`botocore` are faked via sys.modules rather than installed — this
provider's SDK is deliberately excluded from requirements-dev.txt (see
libs/llm_client/requirements.txt), so these tests must not require a real
install, and never make a real AWS call or need a credential.
"""
import sys
import types

import pytest

from libs.llm_client.errors import ProviderNotConfiguredError, ProviderTimeoutError, ProviderTransientError


class _FakeClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class _FakeTimeoutError(Exception):
    pass


def _install_fake_boto3(monkeypatch, *, converse_result=None, converse_error=None):
    """Registers fake boto3/botocore modules in sys.modules so the provider's
    lazy `import boto3` (inside complete()) resolves to test doubles.
    """
    calls = {}

    class _FakeBedrockClient:
        def converse(self, **kwargs):
            calls["converse_kwargs"] = kwargs
            if converse_error is not None:
                raise converse_error
            return converse_result

    def _client(service_name, **kwargs):
        calls["client_args"] = {"service_name": service_name, **kwargs}
        return _FakeBedrockClient()

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = _client

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
    return calls


def _converse_response(text="hello", input_tokens=12, output_tokens=34):
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
    }


# --- config validation -------------------------------------------------------


def test_missing_model_id_raises_not_configured():
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    with pytest.raises(ProviderNotConfiguredError):
        BedrockProvider(model_id=None, region="us-east-1")


def test_placeholder_model_id_raises_not_configured():
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    with pytest.raises(ProviderNotConfiguredError):
        BedrockProvider(model_id="changeme", region="us-east-1")


def test_missing_region_raises_not_configured():
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    with pytest.raises(ProviderNotConfiguredError):
        BedrockProvider(model_id="anthropic.claude-3-5-sonnet", region=None)


def test_reads_config_from_env_when_not_passed_explicitly(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    provider = BedrockProvider()
    assert provider._model_id == "anthropic.claude-3-5-sonnet"
    assert provider._region == "us-west-2"


# --- successful completion ---------------------------------------------------


def test_complete_parses_converse_response_into_provider_response(monkeypatch):
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    calls = _install_fake_boto3(monkeypatch, converse_result=_converse_response("hi there", 7, 9))
    provider = BedrockProvider(model_id="anthropic.claude-3-5-sonnet", region="us-east-1")

    result = provider.complete("a prompt", timeout=10, max_tokens=256)

    assert result.text == "hi there"
    assert result.input_tokens == 7
    assert result.output_tokens == 9
    assert calls["client_args"]["service_name"] == "bedrock-runtime"
    assert calls["client_args"]["region_name"] == "us-east-1"
    assert calls["converse_kwargs"]["modelId"] == "anthropic.claude-3-5-sonnet"
    assert calls["converse_kwargs"]["messages"] == [{"role": "user", "content": [{"text": "a prompt"}]}]
    assert calls["converse_kwargs"]["inferenceConfig"] == {"maxTokens": 256}


def test_complete_joins_multiple_text_blocks(monkeypatch):
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    response = {
        "output": {"message": {"content": [{"text": "part one "}, {"text": "part two"}]}},
        "usage": {"inputTokens": 1, "outputTokens": 2},
    }
    _install_fake_boto3(monkeypatch, converse_result=response)
    provider = BedrockProvider(model_id="anthropic.claude-3-5-sonnet", region="us-east-1")

    result = provider.complete("prompt", timeout=10, max_tokens=256)

    assert result.text == "part one part two"


# --- error translation --------------------------------------------------------


def test_timeout_is_translated_to_provider_timeout_error(monkeypatch):
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    _install_fake_boto3(monkeypatch, converse_error=_FakeTimeoutError("timed out"))
    provider = BedrockProvider(model_id="anthropic.claude-3-5-sonnet", region="us-east-1")

    with pytest.raises(ProviderTimeoutError):
        provider.complete("prompt", timeout=1, max_tokens=256)


@pytest.mark.parametrize(
    "error_code",
    [
        "ThrottlingException",
        "ModelTimeoutException",
        "ServiceUnavailableException",
        "ModelNotReadyException",
        "InternalServerException",
    ],
)
def test_retryable_client_errors_are_translated_to_provider_transient_error(monkeypatch, error_code):
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    _install_fake_boto3(monkeypatch, converse_error=_FakeClientError(error_code))
    provider = BedrockProvider(model_id="anthropic.claude-3-5-sonnet", region="us-east-1")

    with pytest.raises(ProviderTransientError):
        provider.complete("prompt", timeout=10, max_tokens=256)


@pytest.mark.parametrize(
    "error_code",
    [
        "AccessDeniedException",
        "ValidationException",
        "ResourceNotFoundException",
        "ModelErrorException",
        "ServiceQuotaExceededException",
    ],
)
def test_non_retryable_client_error_propagates_unchanged(monkeypatch, error_code):
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    _install_fake_boto3(monkeypatch, converse_error=_FakeClientError(error_code))
    provider = BedrockProvider(model_id="anthropic.claude-3-5-sonnet", region="us-east-1")

    with pytest.raises(_FakeClientError):
        provider.complete("prompt", timeout=10, max_tokens=256)


# --- wiring through LLMClient / _build_provider -------------------------------


def test_build_provider_resolves_bedrock_by_name(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    from libs.llm_client.client import _build_provider
    from libs.llm_client.providers.bedrock_provider import BedrockProvider

    assert isinstance(_build_provider("bedrock"), BedrockProvider)


def test_unknown_provider_error_message_mentions_bedrock():
    from libs.llm_client.client import _build_provider

    with pytest.raises(ValueError, match="bedrock"):
        _build_provider("not-a-real-provider")
