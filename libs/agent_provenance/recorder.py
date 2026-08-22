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

WHAT "ORDERED" MEANS, AND WHAT IT DELIBERATELY DOES NOT COVER

`is_ordered`/`is_grounded`/`is_acceptable` model the REAL, successful,
evidence-grounded path only — see `Stage`'s docstring for the exact shape,
taken from the actual agent-loop runtimes this repo runs. A fallback or
provider-error trace is a genuinely different, much shorter shape (in the
real runtimes, a `PROVIDER_ERROR`/`MAX_TURNS` termination returns a safe
canned reply immediately — it never reaches `draft`, `validation` or
`review` at all). Do not expect a fallback/error trace to satisfy
`is_ordered()`/`is_complete()`/`is_acceptable()`; judge it only by whatever
minimal shape the caller actually needs from it (e.g. that `display` is
present and correctly labelled `fallback`, never presented as model output).
Conflating the two shapes into one grammar would either reject every
legitimate fallback or silently loosen the real path's guarantees to
accommodate one — this module keeps them separate on purpose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class Stage(str, Enum):
    """The eight stages the client requires one trace to cover.

    Declaration order matches the REAL successful path, grounded in the two
    actual agent-loop implementations this repo runs today —
    `libs/eligibility_agent/runtimes/raw_bedrock.py` and
    `.../langchain_runtime.py` — not an invented abstract sequence. Both
    agree on the same shape, because a bounded Bedrock tool-calling loop only
    has one shape:

        request
        { provider_call -> agent_decision(tool_use) -> retrieval }  x0-or-more
        provider_call -> agent_decision(final)
        draft -> validation -> review -> display

    `provider_call` is the actual Converse round-trip; its RESPONSE is what
    determines the agent_decision that follows it — raw_bedrock.py branches
    on `if not response.tool_calls`, langchain_runtime.py's
    `route_after_agent` branches on the same emptiness check on the model
    message's `tool_calls`. So the decision is always downstream of a call,
    never the other way around — an `agent_decision` with no preceding
    `provider_call` cannot happen in the real loop, and is not accepted here.

    `retrieval` happens only when a decision selected a tool (raw_bedrock.py
    then runs `tool.invoke(...)`; langchain_runtime.py's `tools_node` does the
    same) — it never happens before the first `agent_decision`, and it never
    happens after a FINAL decision, because a final decision is exactly the
    case where there is no tool call to execute.

    See `TraceRecorder.is_ordered` for the state machine that enforces this
    shape, and `is_grounded` for why at least one `retrieval` is additionally
    required of the REAL path specifically (a fallback/error trace is judged
    separately — see the module-level note below)."""

    REQUEST = "request"
    PROVIDER_CALL = "provider_call"
    AGENT_DECISION = "agent_decision"
    RETRIEVAL = "retrieval"
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

    def provider_call(self, *, label: ProvenanceLabel, model_id: Optional[str],
                      latency_ms: Optional[int] = None,
                      error_type: Optional[str] = None) -> StageEvent:
        """The actual Converse round-trip (raw_bedrock.py's
        `self._model.converse(...)`; langchain_runtime.py's
        `bound_model.invoke(...)`). Its response is what the FOLLOWING
        `agent_decision` is derived from — record this first.
        `error_type` is a class name. A provider's error MESSAGE can quote the
        payload that caused it, which is why only the type is accepted."""
        return self.record(
            Stage.PROVIDER_CALL,
            provenance_label=label.value,
            model_id=model_id,
            latency_ms=latency_ms,
            error_type=error_type,
        )

    def agent_decision(self, *, tool_name: Optional[str], turn: int,
                       stop_reason: Optional[str] = None) -> StageEvent:
        """The decision the PRECEDING `provider_call`'s response revealed —
        never call this before the `provider_call` it describes.

        `tool_name` set (or `stop_reason == "tool_use"`) means the model chose
        a tool: raw_bedrock.py's `if response.tool_calls:` /
        langchain_runtime.py's `route_after_agent` returning `"tools"`. A
        `retrieval` event recording that tool's actual execution must follow
        immediately.

        `tool_name=None` (and `stop_reason` anything other than `"tool_use"`,
        typically Bedrock's own `"end_turn"`) means the FINAL decision — the
        model had no more tool calls (`if not response.tool_calls:` /
        `route_after_agent` returning `"end"`). No `retrieval` follows a final
        decision; `draft` must come next."""
        return self.record(Stage.AGENT_DECISION, tool_name=tool_name,
                           turn=turn, stop_reason=stop_reason)

    def retrieval(self, *, document_count: int, citation_ids: list,
                  categories: list, excluded_count: int = 0) -> StageEvent:
        """The tool call the immediately preceding `agent_decision` selected,
        actually executed (raw_bedrock.py's `tool.invoke(...)`;
        langchain_runtime.py's `tools_node`). Never a general "gather
        evidence" step at the top of the trace — it only ever follows a
        tool-use decision. Counts, ids and categories — never the documents
        themselves."""
        return self.record(
            Stage.RETRIEVAL,
            document_count=document_count,
            citation_ids=list(citation_ids),
            categories=list(categories),
            excluded_count=excluded_count,
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

    # Internal states for is_ordered()'s walk. Named for what comes NEXT, not
    # what already happened — "_AWAIT_PROVIDER_CALL" is the state in which the
    # next event, if any, must be a provider_call.
    _AWAIT_REQUEST = "await_request"
    _AWAIT_PROVIDER_CALL = "await_provider_call"
    _AWAIT_DECISION = "await_decision"
    _AWAIT_RETRIEVAL = "await_retrieval"
    _AWAIT_DRAFT = "await_draft"
    _AWAIT_VALIDATION = "await_validation"
    _AWAIT_REVIEW = "await_review"
    _AWAIT_DISPLAY = "await_display"
    _DONE = "done"

    @staticmethod
    def _is_tool_decision(event: StageEvent) -> bool:
        """True for a decision to call a tool, false for the FINAL decision
        that ends the loop. `tool_name` set is the authoritative signal — it
        mirrors `if response.tool_calls:` in raw_bedrock.py exactly.
        `stop_reason == "tool_use"` (Bedrock's own Converse `stopReason`) is
        accepted as an alternative signal for a caller that has it but not
        yet a resolved tool name."""
        return (
            event.attributes.get("tool_name") is not None
            or event.attributes.get("stop_reason") == "tool_use"
        )

    def is_ordered(self) -> bool:
        """True when the trace follows the REAL required path exactly (see
        `Stage`'s docstring): a walk through an explicit state machine, not a
        generic "canonical rank" comparison — the loop segment is a genuine
        cycle (provider_call -> agent_decision -> maybe retrieval -> back to
        provider_call), not a linearly-ranked sequence, so no single rank
        ordering could describe it correctly.

        request
          -> provider_call -> agent_decision
               -> [tool_use]  -> retrieval -> back to provider_call
               -> [final]     -> draft -> validation -> review -> display

        Concretely, this rejects (among other malformed shapes):

          * anything before the FIRST event other than `request`, or anything
            after `request` other than `provider_call` — an `agent_decision`
            with no preceding `provider_call` cannot happen in the real loop
            ("provider-before-decision": the call always comes first);
          * an `agent_decision` classified `tool_use` NOT immediately followed
            by `retrieval` (a decision to call a tool with no record of that
            tool ever running — "missing retrieval");
          * anything other than a fresh `provider_call` immediately after a
            `retrieval` — the loop always closes a tool cycle by calling the
            model again, never by going straight to `draft` ("unfinished tool
            cycle");
          * an `agent_decision` classified FINAL not immediately followed by
            `draft` ("final-answer-before-draft") — including a further
            `provider_call`/`retrieval` after it, which would mean the loop
            kept going after supposedly finishing;
          * any loop-stage event (`provider_call`, `agent_decision`,
            `retrieval`) once `draft` has occurred ("post-draft agent
            activity");
          * a repeat of `draft`, `validation`, `review` or `display` — each
            occurs at most once, in that exact order.

        Silently accepts a trace that simply stops partway through a
        well-formed prefix (e.g. just `[request, provider_call]`) — that is
        `is_complete()`'s job to flag as missing stages, not this one's.
        """
        state = self._AWAIT_REQUEST

        for event in self.events:
            stage = event.stage

            if state == self._AWAIT_REQUEST:
                if stage is not Stage.REQUEST:
                    return False
                state = self._AWAIT_PROVIDER_CALL

            elif state == self._AWAIT_PROVIDER_CALL:
                if stage is not Stage.PROVIDER_CALL:
                    return False
                state = self._AWAIT_DECISION

            elif state == self._AWAIT_DECISION:
                if stage is not Stage.AGENT_DECISION:
                    return False
                state = self._AWAIT_RETRIEVAL if self._is_tool_decision(event) else self._AWAIT_DRAFT

            elif state == self._AWAIT_RETRIEVAL:
                if stage is not Stage.RETRIEVAL:
                    return False
                state = self._AWAIT_PROVIDER_CALL

            elif state == self._AWAIT_DRAFT:
                if stage is not Stage.DRAFT:
                    return False
                state = self._AWAIT_VALIDATION

            elif state == self._AWAIT_VALIDATION:
                if stage is not Stage.VALIDATION:
                    return False
                state = self._AWAIT_REVIEW

            elif state == self._AWAIT_REVIEW:
                if stage is not Stage.REVIEW:
                    return False
                state = self._AWAIT_DISPLAY

            elif state == self._AWAIT_DISPLAY:
                if stage is not Stage.DISPLAY:
                    return False
                state = self._DONE

            else:  # self._DONE — nothing may follow display
                return False

        return True

    def is_grounded(self) -> bool:
        """True when at least one `retrieval` occurred. The required path is
        evidence-grounded by construction: a "successful" run that answered
        without ever calling a tool (a real, structurally valid zero-iteration
        walk through `is_ordered`'s state machine — request straight to a
        FINAL decision) is not the real, grounded path the client requires,
        even though nothing about its ordering is wrong. This is checked
        separately from `is_ordered` so a test/caller can tell "wrong shape"
        apart from "right shape, no evidence" — two different failures with
        two different fixes."""
        return any(e.stage is Stage.RETRIEVAL for e in self.events)

    def is_acceptable(self) -> bool:
        """The acceptance bar for one end-to-end REAL/grounded trace: all
        eight stages present, in the exact required order, with at least one
        retrieval. Does not apply to a fallback/error trace — see the module
        docstring."""
        return self.is_complete() and self.is_ordered() and self.is_grounded()
