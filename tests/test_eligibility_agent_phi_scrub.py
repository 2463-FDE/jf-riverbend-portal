"""P6 (w8-planner-2): targeted tests for the Safe Harbor scrub wired into
RawBedrockAgentRuntime (libs/eligibility_agent/runtimes/raw_bedrock.py) —
the smallest real PHI-bearing external LLM provider path in this codebase.
The caller's raw chat message used to reach Bedrock with no scrubbing at
all; libs/deid/safe_harbor.py's scrub() now runs immediately before the
message enters the provider prompt, on both handle_message and its
streaming counterpart.

Not a claim of complete Safe Harbor de-identification — see that module's
own docstring for what pattern-based scrubbing can and cannot close.
"""
import logging
from datetime import datetime, timezone

import pytest

from libs.eligibility_agent.bedrock_tool_port import ConverseTurn, ToolCapableModel
from libs.eligibility_agent.contracts import TerminationReason, VisitContext
from libs.eligibility_agent.memory import VisitMemoryPort
from libs.eligibility_agent.runtimes import raw_bedrock
from libs.eligibility_agent.runtimes.raw_bedrock import RawBedrockAgentRuntime


class FakeVisitMemory(VisitMemoryPort):
    def __init__(self):
        self._store = {}

    def get(self, visit_id):
        return self._store.get(visit_id)

    def put(self, context):
        self._store[context.visit_id] = context


class RecordingModel(ToolCapableModel):
    """Records every `messages` list it was called with, and always answers
    with a plain text turn — no tool call, so the loop ends in one turn."""

    def __init__(self):
        self.calls: list = []

    def converse(self, messages, tools, *, timeout):
        self.calls.append(messages)
        return ConverseTurn(text="ok", tool_calls=[])


class NeverCalledModel(ToolCapableModel):
    def converse(self, messages, tools, *, timeout):
        raise AssertionError("provider must not be called when scrubbing fails")


def _now():
    return datetime.now(timezone.utc)


def _first_message_text(calls) -> str:
    return calls[0][0]["content"][0]["text"]


def test_direct_identifiers_are_removed_before_the_provider_mock_sees_the_request():
    model = RecordingModel()
    runtime = RawBedrockAgentRuntime(memory=FakeVisitMemory(), model=model)

    runtime.handle_message("visit-1", "my SSN is 412-55-9981, please help")

    assert len(model.calls) == 1
    sent_text = _first_message_text(model.calls)
    assert "412-55-9981" not in sent_text
    assert "[REDACTED]" in sent_text


def test_known_patient_names_are_removed():
    model = RecordingModel()
    runtime = RawBedrockAgentRuntime(memory=FakeVisitMemory(), model=model)

    runtime.handle_message(
        "visit-1", "This is regarding Maria Gonzalez's visit", known_identifiers=["Maria", "Gonzalez"]
    )

    sent_text = _first_message_text(model.calls)
    assert "Maria" not in sent_text
    assert "Gonzalez" not in sent_text


def test_provider_is_not_called_when_scrubbing_fails(monkeypatch):
    def _boom(text, known_identifiers):
        raise RuntimeError("scrub exploded")

    monkeypatch.setattr(raw_bedrock, "scrub", _boom)
    model = NeverCalledModel()
    runtime = RawBedrockAgentRuntime(memory=FakeVisitMemory(), model=model)

    result = runtime.handle_message("visit-1", "hello")

    assert result.termination_reason == TerminationReason.PROVIDER_ERROR
    assert result.reply == raw_bedrock._SAFE_SCRUB_ERROR_REPLY


def test_provider_is_not_called_when_scrubbing_fails_streaming(monkeypatch):
    def _boom(text, known_identifiers):
        raise RuntimeError("scrub exploded")

    monkeypatch.setattr(raw_bedrock, "scrub", _boom)
    model = NeverCalledModel()
    runtime = RawBedrockAgentRuntime(memory=FakeVisitMemory(), model=model)

    events = list(runtime.handle_message_stream("visit-1", "hello"))

    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].termination_reason == TerminationReason.PROVIDER_ERROR


def test_scrub_reports_and_logs_contain_no_original_phi(caplog):
    model = RecordingModel()
    runtime = RawBedrockAgentRuntime(memory=FakeVisitMemory(), model=model)

    with caplog.at_level(logging.INFO, logger="libs.eligibility_agent.runtimes.raw_bedrock"):
        runtime.handle_message("visit-1", "reach me at 412-555-0199 or jane.doe@example.com")

    scrub_records = [r for r in caplog.records if "scrubbed before provider call" in r.getMessage()]
    assert scrub_records, "expected a scrub summary log line"
    for record in scrub_records:
        message = record.getMessage()
        assert "412-555-0199" not in message
        assert "jane.doe@example.com" not in message
        # Only category:name=count pairs, never a removed value.
        assert "removed" in message


def test_existing_safe_requests_still_reach_the_provider():
    model = RecordingModel()
    runtime = RawBedrockAgentRuntime(memory=FakeVisitMemory(), model=model)

    result = runtime.handle_message("visit-1", "What does my plan cover for an annual physical?")

    assert len(model.calls) == 1
    assert _first_message_text(model.calls) == "What does my plan cover for an annual physical?"
    assert result.termination_reason == TerminationReason.ANSWERED
    assert result.reply == "ok"
