"""Default AgentRuntime: an explicit Bedrock Converse tool-calling loop.

No framework — a hand-written loop over ToolCapableModel.converse()/
converse_stream(), per the approved plan's "no framework" requirement for
the default runtime. Turns are bounded by a plain `for` loop over a fixed
range (not a manually-incremented counter that could be gotten wrong), so
termination is structurally guaranteed, not just intended. Every tool call
is dispatched through an explicit allowlist and strict Pydantic argument
validation before the tool ever runs; a provider failure or an exhausted
turn budget always produces a safe, generic reply rather than raising or
leaking any diagnostic detail to the user.

handle_message and handle_message_stream (w-9-2-planner P1b) share the same
turn-bounded loop shape and the same tool-dispatch/persistence-scoping rule
via _dispatch_tool_call below — a single source of truth for "only a
VERIFIED verify_current_eligibility outcome may update eligibility_status",
so the two loops cannot drift into disagreeing about it.

The loop shape, turn budget, authorization, visit scope and persistence rule
are unchanged; what the model no longer decides is (a) WHICH tool may run —
the classified intent narrows the offered tool list and is re-enforced at
dispatch — and (b) WHAT THE CALLER READS once a tool has run, which is
rendered from the tool payload itself. See
libs/eligibility_agent/response_contract.py for both.
"""
from datetime import datetime, timezone
from typing import Iterable, Iterator, List, Optional

from libs.agent_budget import BudgetExceededError, preflight_check
from libs.deid.safe_harbor import scrub
from libs.llm_client.errors import LLMClientError
from libs.safe_logging import get_safe_logger
from libs.tracing.spans import record_event, safe_span

from ..bedrock_tool_port import BedrockConverseToolModel, ConverseTurn, ToolCapableModel
from ..contracts import (
    EligibilityStatus,
    TerminationReason,
    UsageTurn,
    VisitContext,
    VisitStreamEvent,
    VisitTurnResult,
    parse_as_of,
)
from ..eligibility_tool import (
    COVERAGE_TOOL_NAME,
    VERIFY_TOOL_NAME,
    EligibilityToolConfig,
    GetCoverageOnFileTool,
    VerifyCurrentEligibilityTool,
)
from ..memory import VisitMemoryPort
from ..response_contract import classify_intent, render_reply

log = get_safe_logger(__name__)

_TRACER_NAME = "eligibility_agent"
# Every tool that exists. The set actually offered to (and runnable by) the
# model on a given turn is the classified intent's own narrower subset — see
# response_contract.IntentDecision and _dispatch_tool_call below.
_ALLOWED_TOOLS = frozenset({VERIFY_TOOL_NAME, COVERAGE_TOOL_NAME})

_SAFE_PROVIDER_ERROR_REPLY = (
    "I couldn't reach the eligibility assistant just now. Please try again in a "
    "moment, or check eligibility manually."
)
_SAFE_MAX_TURNS_REPLY = (
    "I wasn't able to finish checking this in the time I'm allowed. Please try "
    "again, or check eligibility manually."
)
_SAFE_SCRUB_ERROR_REPLY = (
    "I couldn't process that message safely. Please try again, or check "
    "eligibility manually."
)
_SAFE_BUDGET_REPLY = (
    "That message is too long for me to check right now. Please shorten it, "
    "or check eligibility manually."
)
_METRICS_USE_CASE = "eligibility_agent_chat"


class RawBedrockAgentRuntime:
    def __init__(
        self,
        *,
        memory: VisitMemoryPort,
        model: Optional[ToolCapableModel] = None,
        max_turns: int = 4,
        timeout_seconds: float = 20.0,
        tool_config: Optional[EligibilityToolConfig] = None,
        tool_transport=None,
        now=lambda: datetime.now(timezone.utc),
    ):
        self._memory = memory
        self._model = model if model is not None else BedrockConverseToolModel()
        self._max_turns = max_turns
        self._timeout_seconds = timeout_seconds
        self._tool_config = tool_config
        self._tool_transport = tool_transport
        self._now = now

    def _tools_for(self, context: VisitContext) -> dict:
        return {
            VERIFY_TOOL_NAME: VerifyCurrentEligibilityTool(
                context, config=self._tool_config, transport=self._tool_transport
            ),
            COVERAGE_TOOL_NAME: GetCoverageOnFileTool(context),
        }

    def _dispatch_tool_call(self, call, tools_by_name: dict, context: VisitContext, allowed_names=_ALLOWED_TOOLS):
        """Returns (payload, ok, context) for one tool call. `context` is
        the SAME object passed in unless this call is a VERIFIED
        verify_current_eligibility outcome, in which case it is the newly
        persisted copy — see the module docstring for why this is the one
        place that decision is made, shared by both loops below.

        `allowed_names` is the classified intent's own tool set (see
        libs/eligibility_agent/response_contract.py). Narrowing the offered
        tool list is not enough on its own: a model can name a tool it was
        never offered, so the same narrowing is enforced again here, at the
        only point a tool can actually run."""
        if call.name not in _ALLOWED_TOOLS:
            log.warning("agent tool call rejected (reason=unknown_tool)")
            return {"error": "unknown_tool"}, False, context
        if call.name not in allowed_names:
            log.warning("agent tool call rejected (reason=outside_requested_intent tool=%s)", call.name)
            return {"error": "tool_not_requested"}, False, context

        # This surface's closest analogue to "retrieval": a bounded external
        # call fetching payer/coverage evidence, real I/O with real
        # duration. No correlation_id attribute here (unlike the summary
        # agent/Policy Navigator equivalents) — threading one through would
        # mean widening the AgentRuntime protocol both this runtime and the
        # unused langchain_runtime.py comparison spike implement; this span
        # still nests correctly under its request's trace via Tempo's own
        # trace_id grouping, just without its own Loki cross-link.
        with safe_span(_TRACER_NAME, "eligibility_agent.tool_call", {"tool_name": call.name}) as span:
            result = tools_by_name[call.name].invoke(call.arguments)
            payload = result.payload
            span.set_attribute("outcome", payload.get("outcome", "unknown") if result.ok else "rejected")
            if not result.ok:
                log.warning("agent tool call rejected (reason=%s)", payload.get("error", "invalid"))
                return payload, False, context

        # Only a VERIFIED (genuinely new, live) outcome from
        # verify_current_eligibility may update eligibility_status/
        # eligibility_checked_at. get_coverage_on_file is a pure read of
        # what's already stored — persisting its payload here would let a
        # stored-lookup masquerade as a fresh check. simulated/unavailable
        # outcomes echo the stored status back rather than asserting a new
        # one, so they must not overwrite it either — see
        # eligibility_tool.py's own docstring on this.
        if call.name == VERIFY_TOOL_NAME and payload.get("outcome") == "verified":
            status = EligibilityStatus(payload["status"])
            # Persist the payer's real verification time (as_of), NOT
            # now() — a stale fallback carries its original, older
            # checked_at and must not look freshly checked. Absent/
            # unparseable as_of preserves the prior time.
            checked_at = parse_as_of(payload.get("as_of")) or context.eligibility_checked_at
            context = context.model_copy(
                update={
                    "eligibility_status": status,
                    "eligibility_checked_at": checked_at,
                    "updated_at": self._now(),
                }
            )
            self._memory.put(context)
        return payload, True, context

    def handle_message(
        self, visit_id: str, user_message: str, *, known_identifiers: Iterable[str] = ()
    ) -> VisitTurnResult:
        context = self._memory.get(visit_id) or VisitContext(visit_id=visit_id, updated_at=self._now())
        tools_by_name = self._tools_for(context)

        # W10 Metrics Stage 4: the centrally enforced request bound
        # (libs/agent_budget) is checked on the GROSS, unscrubbed message —
        # before scrubbing, before any provider egress — so an oversized
        # request never reaches the model at all, not even for a first turn.
        try:
            preflight_check(_METRICS_USE_CASE, getattr(self._model, "model_id", None), user_message)
        except BudgetExceededError as exc:
            log.warning("agent message rejected by preflight budget (reason=%s)", exc.reason)
            return VisitTurnResult(
                visit_id=visit_id,
                reply=_SAFE_BUDGET_REPLY,
                tool_called=False,
                eligibility_status=context.eligibility_status,
                termination_reason=TerminationReason.BUDGET_REJECTED,
                turns_used=0,
            )

        # P6 (w8-planner-2): the caller's raw free-text chat message used to
        # reach the provider prompt below with no scrubbing at all — see
        # libs/deid/safe_harbor.py. `known_identifiers` lets a future caller
        # pass the visit's patient's own name parts; none does today, so
        # pattern-based categories (SSN, phone, email, dates, etc.) are what
        # actually fires. Fail closed: a scrub failure must not fall back to
        # sending the unscrubbed original.
        try:
            user_message, deid_report = scrub(user_message, known_identifiers)
        except Exception as exc:
            log.warning("agent message scrub failed, refusing provider call (error_type=%s)", type(exc).__name__)
            return VisitTurnResult(
                visit_id=visit_id,
                reply=_SAFE_SCRUB_ERROR_REPLY,
                tool_called=False,
                eligibility_status=context.eligibility_status,
                termination_reason=TerminationReason.PROVIDER_ERROR,
                turns_used=0,
            )
        if deid_report:
            # Categories/counts only, per DeidReport's own contract — never
            # the removed values, never the message itself.
            log.info("agent message scrubbed before provider call (%s)", deid_report.summary())

        messages: list = [{"role": "user", "content": [{"text": user_message}]}]
        tool_called = False
        eligibility_status: Optional[EligibilityStatus] = context.eligibility_status
        usage_events: List[UsageTurn] = []
        model_id = getattr(self._model, "model_id", None)
        # Classified on the SCRUBBED message: scrubbing removes identifiers,
        # not question words, so intent survives it and no raw identifier is
        # ever read here.
        decision = classify_intent(user_message)
        results = _ToolResults()

        for turn in range(1, self._max_turns + 1):
            with safe_span(_TRACER_NAME, "eligibility_agent.provider_call", {"turn": turn}) as span:
                try:
                    response = self._model.converse(messages, decision.tool_specs, timeout=self._timeout_seconds)
                except LLMClientError as exc:
                    # The provider-error base — covers timeout, transient, and the
                    # non-retryable/response-shape failures the tool port now
                    # normalizes to ProviderCallError. Any of them must degrade to
                    # a safe PROVIDER_ERROR turn, never escape handle_message.
                    log.warning("agent provider call failed (turn=%s, error_type=%s)", turn, type(exc).__name__)
                    span.record_exception_type(type(exc).__name__)
                    return VisitTurnResult(
                        visit_id=visit_id,
                        reply=_SAFE_PROVIDER_ERROR_REPLY,
                        tool_called=tool_called,
                        eligibility_status=eligibility_status,
                        termination_reason=TerminationReason.PROVIDER_ERROR,
                        turns_used=turn,
                        usage=tuple(usage_events),
                    )

                if response.input_tokens is not None or response.output_tokens is not None:
                    usage_events.append(UsageTurn(
                        model_id=model_id, turn=turn,
                        input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                    ))

                stop_reason = "tool_use" if response.tool_calls else "end_turn"
                # A decision is a point-in-time read of the response just
                # received, not an operation with its own duration — an
                # event on this real provider-call span, not a second span.
                record_event(span, "agent_decision", {"turn": turn, "stop_reason": stop_reason})

                if not response.tool_calls:
                    return VisitTurnResult(
                        visit_id=visit_id,
                        # Once a tool has run, its result is rendered from the
                        # payload itself — the model's prose for this turn is
                        # discarded rather than trusted to describe an
                        # outcome it could soften or overstate.
                        reply=results.render(decision) or response.text or "",
                        tool_called=tool_called,
                        eligibility_status=eligibility_status,
                        termination_reason=TerminationReason.ANSWERED,
                        turns_used=turn,
                        usage=tuple(usage_events),
                    )

            messages.append({"role": "assistant", "content": _assistant_content(response)})
            tool_result_blocks = []
            for call in response.tool_calls:
                payload, ok, context = self._dispatch_tool_call(
                    call, tools_by_name, context, decision.tool_names
                )
                if ok:
                    tool_called = True
                    eligibility_status = context.eligibility_status
                    results.record(call.name, payload)
                tool_result_blocks.append(
                    {"toolResult": {"toolUseId": call.id, "content": [{"json": payload}]}}
                )
            messages.append({"role": "user", "content": tool_result_blocks})

        return VisitTurnResult(
            visit_id=visit_id,
            reply=_SAFE_MAX_TURNS_REPLY,
            tool_called=tool_called,
            eligibility_status=eligibility_status,
            termination_reason=TerminationReason.MAX_TURNS,
            turns_used=self._max_turns,
            usage=tuple(usage_events),
        )

    def handle_message_stream(
        self, visit_id: str, user_message: str, *, known_identifiers: Iterable[str] = ()
    ) -> Iterator[VisitStreamEvent]:
        """w-9-2-planner P1b: same bounded loop and dispatch rule as
        handle_message, but forwards each text_delta from the model AS IT
        ARRIVES instead of buffering a complete reply. Tool calls are never
        forwarded — only dispatched internally, exactly like handle_message
        — so a tool-resolution turn (the normal case for this agent)
        produces no output at all until the model's actual answer starts
        streaming; the caller may show a spinner in the meantime, per
        agents.md.

        Ends in exactly one terminal event: "done" (safe categorical
        metadata, mirroring VisitTurnResult) or "error" (one sanitized
        message, never a partial answer represented as complete). If the
        caller simply stops iterating (a client disconnect), this generator
        is closed by Python at its current `yield` — the `finally` in
        BedrockConverseToolModel.converse_stream already closes the
        underlying provider stream, and no further tool/model calls happen
        for this visit.

        P6 (w8-planner-2): scrubs `user_message` the same way and for the
        same reason handle_message does — see its comment. A scrub failure
        yields one "error" event and never reaches the provider."""
        context = self._memory.get(visit_id) or VisitContext(visit_id=visit_id, updated_at=self._now())
        tools_by_name = self._tools_for(context)

        # W10 Metrics Stage 4: same gross, pre-scrub check as handle_message
        # — see that method's identical block for the full rationale.
        try:
            preflight_check(_METRICS_USE_CASE, getattr(self._model, "model_id", None), user_message)
        except BudgetExceededError as exc:
            log.warning("agent message rejected by preflight budget (reason=%s)", exc.reason)
            yield VisitStreamEvent(
                kind="error",
                text=_SAFE_BUDGET_REPLY,
                tool_called=False,
                eligibility_status=context.eligibility_status,
                termination_reason=TerminationReason.BUDGET_REJECTED,
                turns_used=0,
            )
            return

        try:
            user_message, deid_report = scrub(user_message, known_identifiers)
        except Exception as exc:
            log.warning("agent message scrub failed, refusing provider call (error_type=%s)", type(exc).__name__)
            yield VisitStreamEvent(
                kind="error",
                text=_SAFE_SCRUB_ERROR_REPLY,
                tool_called=False,
                eligibility_status=context.eligibility_status,
                termination_reason=TerminationReason.PROVIDER_ERROR,
                turns_used=0,
            )
            return
        if deid_report:
            log.info("agent message scrubbed before provider call (%s)", deid_report.summary())

        messages: list = [{"role": "user", "content": [{"text": user_message}]}]
        tool_called = False
        eligibility_status: Optional[EligibilityStatus] = context.eligibility_status
        usage_events: List[UsageTurn] = []
        model_id = getattr(self._model, "model_id", None)
        decision = classify_intent(user_message)
        results = _ToolResults()

        for turn in range(1, self._max_turns + 1):
            collected_tool_calls = []
            turn_input_tokens = turn_output_tokens = None
            # Duration here is the WHOLE streamed turn, first byte to the
            # tool-use/end-turn decision — mirrors bedrock_tool_port.py's own
            # converse_stream span one layer down (Stage 1's cancellation
            # handling there is unaffected; safe_span's own GeneratorExit
            # handling closes this span cleanly on an early gen.close() too).
            with safe_span(_TRACER_NAME, "eligibility_agent.provider_call", {"turn": turn}) as span:
                try:
                    for event in self._model.converse_stream(
                        messages, decision.tool_specs, timeout=self._timeout_seconds
                    ):
                        # Once a tool has run this turn's model prose is
                        # going to be replaced by the deterministic render,
                        # so it must not reach the browser first — otherwise
                        # the caller would watch an unvetted description of
                        # the outcome appear and then be contradicted.
                        if event.kind == "text_delta" and event.text and not results.any_recorded:
                            yield VisitStreamEvent(kind="delta", text=event.text)
                        elif event.kind == "tool_call" and event.tool_call is not None:
                            collected_tool_calls.append(event.tool_call)
                        elif event.kind == "usage":
                            turn_input_tokens, turn_output_tokens = event.input_tokens, event.output_tokens
                except LLMClientError as exc:
                    log.warning("agent provider call failed (turn=%s, error_type=%s)", turn, type(exc).__name__)
                    span.record_exception_type(type(exc).__name__)
                    yield VisitStreamEvent(
                        kind="error",
                        text=_SAFE_PROVIDER_ERROR_REPLY,
                        tool_called=tool_called,
                        eligibility_status=eligibility_status,
                        termination_reason=TerminationReason.PROVIDER_ERROR,
                        turns_used=turn,
                        usage=tuple(usage_events),
                    )
                    return

                if turn_input_tokens is not None or turn_output_tokens is not None:
                    usage_events.append(UsageTurn(
                        model_id=model_id, turn=turn,
                        input_tokens=turn_input_tokens, output_tokens=turn_output_tokens,
                    ))

                stop_reason = "tool_use" if collected_tool_calls else "end_turn"
                record_event(span, "agent_decision", {"turn": turn, "stop_reason": stop_reason})

                if not collected_tool_calls:
                    # The deterministic result arrives as an ordinary delta,
                    # exactly like any other user-facing text, so the
                    # existing NDJSON contract ("done" never carries reply
                    # text) is unchanged.
                    rendered = results.render(decision)
                    if rendered:
                        yield VisitStreamEvent(kind="delta", text=rendered)
                    yield VisitStreamEvent(
                        kind="done",
                        tool_called=tool_called,
                        eligibility_status=eligibility_status,
                        termination_reason=TerminationReason.ANSWERED,
                        turns_used=turn,
                        usage=tuple(usage_events),
                    )
                    return

            messages.append(
                {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.arguments}} for c in collected_tool_calls
                ]}
            )
            tool_result_blocks = []
            for call in collected_tool_calls:
                payload, ok, context = self._dispatch_tool_call(
                    call, tools_by_name, context, decision.tool_names
                )
                if ok:
                    tool_called = True
                    eligibility_status = context.eligibility_status
                    results.record(call.name, payload)
                tool_result_blocks.append(
                    {"toolResult": {"toolUseId": call.id, "content": [{"json": payload}]}}
                )
            messages.append({"role": "user", "content": tool_result_blocks})

        # w-9-2-planner P1b review fix (STREAM-MAX-TURNS-BLANK): the blocking
        # handle_message above returns _SAFE_MAX_TURNS_REPLY as its reply on
        # this same branch; the streaming path was emitting a bare "done"
        # with no text at all. VisitStreamEvent's own contract (see
        # contracts.py) says "done" never carries reply text — it must
        # arrive as a "delta" first, exactly like every other piece of
        # user-facing answer text.
        yield VisitStreamEvent(kind="delta", text=_SAFE_MAX_TURNS_REPLY)
        yield VisitStreamEvent(
            kind="done",
            tool_called=tool_called,
            eligibility_status=eligibility_status,
            termination_reason=TerminationReason.MAX_TURNS,
            turns_used=self._max_turns,
            usage=tuple(usage_events),
        )


class _ToolResults:
    """The payloads this turn's tools actually produced, kept per tool so the
    deterministic renderer sees each outcome once. Only successful dispatches
    are recorded: a rejected call (unknown tool, arguments the model tried to
    smuggle, a tool outside the requested intent) leaves nothing here, so it
    can never be rendered as though a lookup had happened."""

    def __init__(self):
        self._verify: Optional[dict] = None
        self._coverage: Optional[dict] = None

    @property
    def any_recorded(self) -> bool:
        return self._verify is not None or self._coverage is not None

    def record(self, tool_name: str, payload: dict) -> None:
        if tool_name == VERIFY_TOOL_NAME:
            self._verify = payload
        elif tool_name == COVERAGE_TOOL_NAME:
            self._coverage = payload

    def render(self, decision) -> Optional[str]:
        return render_reply(
            verify_payload=self._verify,
            coverage_payload=self._coverage,
            include_member_id=decision.include_member_id,
        )


def _assistant_content(response: ConverseTurn) -> list:
    blocks = []
    if response.text:
        blocks.append({"text": response.text})
    for call in response.tool_calls:
        blocks.append({"toolUse": {"toolUseId": call.id, "name": call.name, "input": call.arguments}})
    return blocks
