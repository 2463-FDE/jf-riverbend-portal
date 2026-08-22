"""One privacy-safe trace across the eight stages, and a guard that makes the
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
    """The eight stages the client requires one trace to cover, declared in the
    canonical order they must occur along the required path:
    request -> retrieval -> agent_decision -> provider_call -> draft ->
    validation -> review -> display. Enum member order IS the canonical order
    (see is_ordered)."""

    REQUEST = "request"
    RETRIEVAL = "retrieval"
    AGENT_DECISION = "agent_decision"
    PROVIDER_CALL = "provider_call"
    DRAFT = "draft"
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

    def draft(self, *, draft_version: int, label: ProvenanceLabel,
              model_id: Optional[str], prompt_version: Optional[str],
              citation_ids: list) -> StageEvent:
        """The versioned, evidence-based draft — recorded BY REFERENCE only:
        which version, its provenance label, which model and prompt version
        produced it, and which citations it made. Never the draft text: adr/0010
        keeps the text in agent_draft_provenance as the clinical artifact, and
        the telemetry copy stays forbidden (see FORBIDDEN_KEYS)."""
        return self.record(
            Stage.DRAFT,
            draft_version=draft_version,
            provenance_label=label.value,
            model_id=model_id,
            prompt_version=prompt_version,
            citation_ids=list(citation_ids),
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
        """True when all eight stages appear. The client requires ONE trace
        covering the whole path, so a partial trace is a failed acceptance
        criterion, not a partial success."""
        return self.stages_covered() == set(Stage)

    def missing_stages(self) -> list:
        return [s.value for s in Stage if s not in self.stages_covered()]

    # Only these two stages may occur more than once, and only because a
    # bounded tool-calling loop can genuinely call the model and decide on a
    # tool several times before it has enough evidence to draft. Every other
    # stage marks a point the required path passes through exactly once.
    _REPEATABLE_STAGES = frozenset({Stage.AGENT_DECISION, Stage.PROVIDER_CALL})

    def is_ordered(self) -> bool:
        """True when the trace follows the canonical Stage order with no
        stage out of place, under the one narrow exception the required path
        actually has:

        `agent_decision` and `provider_call` may repeat — but ONLY before the
        (unique) `draft` event, only after `retrieval` has already occurred,
        and only as one or more COMPLETE, STRICTLY ALTERNATING pairs starting
        with `agent_decision` and closed by `provider_call`. That is the
        actual shape of a bounded tool-calling loop: decide, call the model,
        maybe decide-and-call again, THEN draft — never a call with no
        decision behind it, never two decisions or two calls back to back,
        and never a draft produced while a decision is still awaiting the
        call that was supposed to act on it. Concretely, this rejects:

          * a `provider_call` with no preceding, not-yet-closed
            `agent_decision` (covers both "provider before any decision" and
            "two provider_calls in a row" — the second has nothing open to
            close);
          * an `agent_decision` while a previous one is still open, i.e. two
            decisions with no `provider_call` closing the first in between;
          * `draft` while a decision is still open (an "unfinished pair") —
            the draft would then rest on a decision the trace never shows
            being acted on.

        A repeat of either loop stage after `draft` has occurred means the
        loop ran again post-hoc — which would mean the drafted version was
        not actually produced by the evidence/decisions the trace claims
        preceded it, so this is treated as an ordering failure too, not a
        tolerated retry.

        Every OTHER stage (request, retrieval, draft, validation, review,
        display) must occur at most once, and the stages that do occur must
        be in non-decreasing canonical rank — the same "no going backward"
        rule as before.
        """
        canonical = list(Stage)
        retrieval_rank = canonical.index(Stage.RETRIEVAL)
        seen_once: set = set()
        draft_seen = False
        last_once_rank = -1
        # Pairing state for the loop segment. True = no decision is currently
        # open — the next loop event, if any, must be a fresh agent_decision
        # (or there may be none at all: the loop is a whole number of pairs).
        # False = an agent_decision is open, awaiting the provider_call that
        # closes it.
        awaiting_decision = True

        for event in self.events:
            stage = event.stage
            if stage in self._REPEATABLE_STAGES:
                if draft_seen:
                    return False  # loop stage recurring after draft: invalid
                if last_once_rank < retrieval_rank:
                    return False  # loop stage before retrieval has occurred: invalid

                if stage is Stage.AGENT_DECISION:
                    if not awaiting_decision:
                        return False  # consecutive decisions: prior one still open
                    awaiting_decision = False
                else:  # Stage.PROVIDER_CALL
                    if awaiting_decision:
                        return False  # provider-before-decision, or two calls in a row
                    awaiting_decision = True
                continue

            if stage in seen_once:
                return False  # a non-loop stage may only occur once
            seen_once.add(stage)

            rank = canonical.index(stage)
            if rank < last_once_rank:
                return False  # went backward relative to prior stages
            last_once_rank = rank
            if stage is Stage.DRAFT:
                if not awaiting_decision:
                    return False  # draft while a decision is still unpaired
                draft_seen = True

        return True

    def is_acceptable(self) -> bool:
        """The acceptance bar for one end-to-end trace: all eight stages present
        AND in canonical order (loop stages repeatable only before draft)."""
        return self.is_complete() and self.is_ordered()
