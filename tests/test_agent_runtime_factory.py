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
