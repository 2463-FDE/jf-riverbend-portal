"""One privacy-safe trace across the seven stages, and a guard that makes the
privacy property enforced rather than intended.

THE PROBLEM THIS SOLVES

The client's constraint is absolute: persist only source id/version, citation
ids, categories, status, timestamps and the correlation id. Never persist
prompts, model output, retrieved text, patient data, identifiers, credentials or
raw provider errors.

A convention cannot hold that line. Someone adds `prompt=...` to a span for a
day's debugging, it ships, and the trace now contains what the trace exists to
avoid. So every attribute passes `assert_safe()` and a forbidden key **raises**.
Failing a request is the correct outcome: a dropped trace is recoverable, a
leaked prompt in a log aggregator is not.

WHY KEYS AND NOT VALUES

The guard rejects by attribute NAME, not by inspecting content. Scanning values
for anything PHI-shaped is the job of `libs.deid`, and doing it here would
invite the belief that anything passing is safe to store. This is a narrow,
predictable rule: these names never appear in a trace, whatever they hold.

`libs.tracing.spans` already sets the pattern — `record_exception_type` records
an exception's TYPE and not its message, precisely so a provider error string
cannot ride out inside telemetry. This module generalises that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class Stage(str, Enum):
    """The seven stages the client requires one trace to cover."""

    REQUEST = "request"
    RETRIEVAL = "retrieval"
    AGENT_DECISION = "agent_decision"
    PROVIDER_CALL = "provider_call"
    VALIDATION = "validation"
    REVIEW = "review"
    DISPLAY = "display"


STAGES = tuple(s.value for s in Stage)


class ProvenanceLabel(str, Enum):
    """Where a result actually came from. The client requires this explicit.

    `FALLBACK` text must never be presented as model output.
    """

    REAL = "real"
    FIXTURE = "fixture"
    FALLBACK = "fallback"


class ForbiddenPayload(ValueError):
    """A trace or provenance attribute carried something that must never persist."""


# Attribute names that may never appear in a trace or a provenance row.
# Deliberately broad on the model-payload side: a false positive costs a
# renamed attribute, a false negative costs a leaked prompt.
FORBIDDEN_KEYS = frozenset(
    {
        # model payloads
        "prompt", "prompts", "system_prompt", "user_prompt", "messages",
        "completion", "response", "response_text", "output", "model_output",
        "generated_text", "draft_text", "summary_text", "text", "content",
        # Near-miss names a caller would plausibly reach for. Added because a
        # test asked for `generated` and the guard did not have it — exact-match
        # on the full key, so `draft_version` and `prompt_version` stay allowed.
        "generated", "generated_summary", "draft", "summary", "reply",
        "body", "raw_body", "raw_response",
        # retrieved material
        "document", "documents", "chunk", "chunks", "retrieved", "retrieved_text",
        "passage", "passages", "snippet", "context",
        # patient data and identifiers
        "ssn", "social_security_number", "dob", "date_of_birth", "mrn",
        "name", "first_name", "last_name", "full_name", "patient_name",
        "address", "street_address", "city", "zip_code", "phone", "email",
        "notes", "clinical_notes", "diagnosis", "allergies", "medications",
        # credentials
        "password", "token", "api_key", "apikey", "secret", "authorization",
        "aws_access_key_id", "aws_secret_access_key", "session_token",
        "internal_service_token", "bedrock_token",
        # provider error detail — the TYPE is fine, the message is not
        "error", "error_message", "exception", "traceback", "stderr",
    }
)


def assert_safe(attributes: Mapping[str, Any]) -> None:
    """Raise if any attribute name is forbidden. Called on every write."""
    offenders = sorted(k for k in attributes if k.lower() in FORBIDDEN_KEYS)
    if offenders:
        raise ForbiddenPayload(
            f"attribute(s) {offenders} may never be traced or persisted. Record a "
            f"reference or a code instead: a source id and version, a citation id, "
            f"a category, a status, or an exception TYPE. See libs/agent_provenance."
        )


@dataclass(frozen=True)
class StageEvent:
    stage: Stage
    attributes: dict = field(default_factory=dict)


@dataclass
class TraceRecorder:
    """Accumulates one request's stage events under a single correlation id.

    Deliberately does not talk to a database or an exporter. It is the shape and
    the guard; wiring it to `libs.tracing` spans and to
    `agent_draft_provenance` is the caller's job, and keeping it pure is what
    lets the guard be tested without infrastructure.
    """

    correlation_id: str
    events: list = field(default_factory=list)

    def record(self, stage: Stage, **attributes: Any) -> StageEvent:
        assert_safe(attributes)
        event = StageEvent(stage=stage, attributes=dict(attributes))
        self.events.append(event)
        return event

    # --- convenience wrappers, one per stage the client named --------------- #

    def request(self, *, actor_role: str) -> StageEvent:
        return self.record(Stage.REQUEST, actor_role=actor_role)

    def retrieval(self, *, document_count: int, citation_ids: list,
                  categories: list, excluded_count: int = 0) -> StageEvent:
        """Counts, ids and categories — never the documents themselves."""
        return self.record(
            Stage.RETRIEVAL,
            document_count=document_count,
            citation_ids=list(citation_ids),
            categories=list(categories),
            excluded_count=excluded_count,
        )

    def agent_decision(self, *, tool_name: Optional[str], turn: int,
                       stop_reason: Optional[str] = None) -> StageEvent:
        """Which tool the model chose and when — not what it said about it."""
        return self.record(Stage.AGENT_DECISION, tool_name=tool_name,
                           turn=turn, stop_reason=stop_reason)

    def provider_call(self, *, label: ProvenanceLabel, model_id: Optional[str],
                      latency_ms: Optional[int] = None,
                      error_type: Optional[str] = None) -> StageEvent:
        """`error_type` is a class name. A provider's error MESSAGE can quote the
        payload that caused it, which is why only the type is accepted."""
        return self.record(
            Stage.PROVIDER_CALL,
            provenance_label=label.value,
            model_id=model_id,
            latency_ms=latency_ms,
            error_type=error_type,
        )

    def validation(self, *, passed: bool, validation_code: Optional[str],
                   citation_ids: list) -> StageEvent:
        """A reason CODE, not a message — a validator message could quote the
        text it rejected."""
        return self.record(Stage.VALIDATION, passed=passed,
                           validation_code=validation_code,
                           citation_ids=list(citation_ids))

    def review(self, *, decision: str, draft_version: int,
               decided_by_user_id: Optional[int]) -> StageEvent:
        """A user id, never a username or a name — an id is a reference, a name
        is an identifier."""
        return self.record(Stage.REVIEW, decision=decision,
                           draft_version=draft_version,
                           decided_by_user_id=decided_by_user_id)

    def display(self, *, draft_version: int, label: ProvenanceLabel) -> StageEvent:
        return self.record(Stage.DISPLAY, draft_version=draft_version,
                           provenance_label=label.value)

    # --- assertions a test or an acceptance run can make ------------------- #

    def stages_covered(self) -> set:
        return {e.stage for e in self.events}

    def is_complete(self) -> bool:
        """True when all seven stages appear. The client requires ONE trace
        covering the whole path, so a partial trace is a failed acceptance
        criterion, not a partial success."""
        return self.stages_covered() == set(Stage)

    def missing_stages(self) -> list:
        return [s.value for s in Stage if s not in self.stages_covered()]
