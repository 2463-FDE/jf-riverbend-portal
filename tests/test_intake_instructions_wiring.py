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


# --- Codex review (2026-08-08, PR #24, medium): bounded timeout/no retries -
#
# The shared LLMConfig defaults (LLM_TIMEOUT_SECONDS=30, LLM_MAX_RETRIES=3)
# can exceed the gateway's own fixed 30s downstream timeout
# (services/gateway/app.py::_post) — a slow/stalled provider would 502 at the
# gateway before this endpoint's safe template fallback could ever return.
# This route must always use its own small, no-retry budget instead.


def test_default_timeout_and_retries_are_bounded_well_under_the_gateway_budget(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("INTAKE_INSTRUCTIONS_LLM_MAX_RETRIES", raising=False)
    # _TIMEOUT_SECONDS/_MAX_RETRIES are read once at module import time — the
    # loaded module under test already reflects the repo's real defaults
    # (8s/0 retries), so this only needs to assert the shape, not re-read env.

    client = wiring.get_llm_client()

    # services/gateway/app.py::_post's hardcoded downstream timeout.
    GATEWAY_DOWNSTREAM_TIMEOUT_SECONDS = 30
    assert client._config.timeout_seconds < GATEWAY_DOWNSTREAM_TIMEOUT_SECONDS
    assert client._config.max_retries == 0


def test_timeout_and_retries_are_configurable_via_env(monkeypatch):
    # Reload the module so its module-level _TIMEOUT_SECONDS/_MAX_RETRIES
    # constants pick up the overridden env vars.
    monkeypatch.setenv("INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("INTAKE_INSTRUCTIONS_LLM_MAX_RETRIES", "1")
    reloaded = load_module("services/intake-service/instructions_wiring.py", "intake_instructions_wiring_reload")

    client = reloaded.get_llm_client()

    assert client._config.timeout_seconds == 3.0
    assert client._config.max_retries == 1
