"""Tests for the fail-closed AgentRuntime factory
(libs/eligibility_agent/runtime.py::build_agent_runtime).

Importing libs.eligibility_agent.runtimes.langchain_runtime here never
requires langchain_core/langgraph installed — only handle_message() and the
two default factory methods import them, and none of those run in this file
(mirrors libs/llm_client/providers/bedrock_provider.py's lazy-boto3 pattern).
"""
import pytest

from libs.eligibility_agent.runtime import build_agent_runtime
from libs.eligibility_agent.runtimes.langchain_runtime import LangChainAgentRuntime
from libs.eligibility_agent.runtimes.raw_bedrock import RawBedrockAgentRuntime


class _FakeMemory:
    def get(self, visit_id):
        return None

    def put(self, context):
        pass


class _FakeModel:
    def converse(self, messages, tools, *, timeout):
        raise NotImplementedError


def test_defaults_to_raw_bedrock_when_env_unset(monkeypatch):
    monkeypatch.delenv("ELIGIBILITY_AGENT_RUNTIME", raising=False)

    runtime = build_agent_runtime(memory=_FakeMemory(), model=_FakeModel())

    assert isinstance(runtime, RawBedrockAgentRuntime)


def test_explicit_name_overrides_env(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "langchain")

    runtime = build_agent_runtime(name="raw_bedrock", memory=_FakeMemory(), model=_FakeModel())

    assert isinstance(runtime, RawBedrockAgentRuntime)


def test_env_var_selects_langchain(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "langchain")

    runtime = build_agent_runtime(
        memory=_FakeMemory(),
        chat_model_factory=lambda: None,
        checkpointer_factory=lambda: None,
    )

    assert isinstance(runtime, LangChainAgentRuntime)


def test_unknown_runtime_name_fails_closed(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "some_other_framework")

    with pytest.raises(ValueError, match="raw_bedrock"):
        build_agent_runtime(memory=_FakeMemory())


def test_empty_env_value_fails_closed_rather_than_silently_defaulting(monkeypatch):
    # A set-but-empty env var is a real, distinguishable misconfiguration —
    # it must not be treated the same as "unset" (which defaults to
    # raw_bedrock); it must fail closed like any other unrecognized value.
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "")

    with pytest.raises(ValueError):
        build_agent_runtime(memory=_FakeMemory())


# --- Stage 2 (feature-readiness): ollama runtime ----------------------------
#
# "ollama" reuses RawBedrockAgentRuntime's own loop — the only difference is
# which ToolCapableModel backs it — so it must return that SAME class, not a
# distinct runtime type.


def test_env_var_selects_ollama_and_reuses_the_raw_bedrock_loop(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")

    runtime = build_agent_runtime(memory=_FakeMemory())

    assert isinstance(runtime, RawBedrockAgentRuntime)
    from libs.eligibility_agent.ollama_tool_port import OllamaToolCapableModel

    assert isinstance(runtime._model, OllamaToolCapableModel)


def test_ollama_runtime_respects_an_explicit_model_override(monkeypatch):
    # A caller-supplied `model` (e.g. a test double, or this factory's own
    # caller wanting a specific instance) must win over the default —
    # OllamaToolCapableModel() is never constructed in that case.
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "ollama")
    fake_model = _FakeModel()

    runtime = build_agent_runtime(memory=_FakeMemory(), model=fake_model)

    assert runtime._model is fake_model


def test_unconfigured_ollama_provider_fails_closed_at_construction(monkeypatch):
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "ollama")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    from libs.llm_client.errors import ProviderNotConfiguredError

    with pytest.raises(ProviderNotConfiguredError):
        build_agent_runtime(memory=_FakeMemory())
