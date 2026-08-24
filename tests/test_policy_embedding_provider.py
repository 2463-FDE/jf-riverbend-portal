"""Tests for the policy corpus's Bedrock embedding provider
(libs/policy_corpus/bedrock_embedding_provider.py). boto3/botocore are faked
via sys.modules (mirrors tests/test_bedrock_tool_port.py) — no network, no
real AWS call. Focus: it never gets selected through
libs/embedding_client's own env-var factory, and its error boundary matches
the rest of this codebase's Bedrock adapters.
"""
import sys
import types

import pytest

from libs.embedding_client.errors import ProviderNotConfiguredError, ProviderTransientError
from libs.policy_corpus import BedrockPolicyEmbeddingProvider


class _FakeClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


def _install_fake_boto3(monkeypatch, *, bodies=None, invoke_error=None):
    calls = []

    class _FakeBody:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            import json

            return json.dumps(self._payload).encode("utf-8")

    class _FakeBedrockClient:
        def invoke_model(self, **kwargs):
            calls.append(kwargs)
            if invoke_error is not None:
                raise invoke_error
            return {"body": _FakeBody(bodies[len(calls) - 1])}

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service_name, **kwargs: _FakeBedrockClient()

    fake_botocore = types.ModuleType("botocore")
    fake_config_mod = types.ModuleType("botocore.config")
    fake_config_mod.Config = lambda **kwargs: kwargs
    fake_exceptions_mod = types.ModuleType("botocore.exceptions")
    fake_exceptions_mod.ClientError = _FakeClientError
    fake_exceptions_mod.ConnectTimeoutError = type("ConnectTimeoutError", (Exception,), {})
    fake_exceptions_mod.ReadTimeoutError = type("ReadTimeoutError", (Exception,), {})

    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", fake_config_mod)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", fake_exceptions_mod)
    return calls


def test_missing_model_id_raises_not_configured():
    with pytest.raises(ProviderNotConfiguredError):
        BedrockPolicyEmbeddingProvider(model_id=None, region="us-east-1")


def test_placeholder_model_id_raises_not_configured():
    with pytest.raises(ProviderNotConfiguredError):
        BedrockPolicyEmbeddingProvider(model_id="changeme", region="us-east-1")


def test_missing_region_raises_not_configured():
    with pytest.raises(ProviderNotConfiguredError):
        BedrockPolicyEmbeddingProvider(model_id="amazon.titan-embed-text-v2:0", region=None)


def test_embeds_each_text_and_sums_token_counts(monkeypatch):
    _install_fake_boto3(
        monkeypatch,
        bodies=[
            {"embedding": [0.1, 0.2], "inputTextTokenCount": 3},
            {"embedding": [0.3, 0.4], "inputTextTokenCount": 5},
        ],
    )
    provider = BedrockPolicyEmbeddingProvider(model_id="amazon.titan-embed-text-v2:0", region="us-east-1")

    response = provider.embed(["first chunk", "second chunk"], timeout=10)

    assert response.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert response.input_tokens == 8


def test_retryable_client_error_becomes_transient(monkeypatch):
    _install_fake_boto3(monkeypatch, invoke_error=_FakeClientError("ThrottlingException"))
    provider = BedrockPolicyEmbeddingProvider(model_id="amazon.titan-embed-text-v2:0", region="us-east-1")

    with pytest.raises(ProviderTransientError):
        provider.embed(["text"], timeout=10)


def test_never_registered_in_embedding_clients_own_provider_factory():
    # The whole point of keeping this adapter separate: flipping
    # EMBEDDING_PROVIDER (used by the patient-encounter corpus) must never
    # be able to select Bedrock — that client's factory only knows fake/ollama.
    from libs.embedding_client.client import _build_provider

    with pytest.raises(ValueError, match="Unknown EMBEDDING_PROVIDER"):
        _build_provider("bedrock")
