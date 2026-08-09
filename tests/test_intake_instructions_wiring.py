"""Tests for intake-service's instructions_wiring.py (Stage 1 — feature
readiness): memoized, safe construction of the LLMClient used by
POST /intake/instructions. Mirrors
tests/test_eligibility_agent_wiring.py's construction-failure-is-cached
shape for eligibility-service's agent runtime.
"""
import pytest

from conftest import load_module

wiring = load_module("services/intake-service/instructions_wiring.py", "intake_instructions_wiring")


@pytest.fixture(autouse=True)
def _reset_module_singletons():
    wiring._client = None
    wiring._client_build_failed = False
    yield
    wiring._client = None
    wiring._client_build_failed = False


def test_default_fake_provider_constructs_successfully(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)  # defaults to "fake"

    client = wiring.get_llm_client()

    assert client is not None


def test_memoizes_the_same_client_instance(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    first = wiring.get_llm_client()
    second = wiring.get_llm_client()

    assert first is second


def test_unconfigured_ollama_provider_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    assert wiring.get_llm_client() is None


def test_unconfigured_provider_failure_is_cached_not_retried(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    calls = {"n": 0}
    real_init = wiring.LLMClient.__init__

    def counting_init(self, *a, **k):
        calls["n"] += 1
        return real_init(self, *a, **k)

    monkeypatch.setattr(wiring.LLMClient, "__init__", counting_init)

    assert wiring.get_llm_client() is None
    assert wiring.get_llm_client() is None
    assert calls["n"] == 1


def test_unknown_provider_name_also_degrades_to_none(monkeypatch):
    # _build_provider raises a bare ValueError (not an LLMClientError) for an
    # unrecognized LLM_PROVIDER — the wiring's broad except must still catch
    # it rather than crashing the endpoint.
    monkeypatch.setenv("LLM_PROVIDER", "not-a-real-provider")

    assert wiring.get_llm_client() is None
