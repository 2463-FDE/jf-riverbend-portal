"""Lazy, safe construction of the Stage 2 AgentRuntime + Redis-backed visit
memory for eligibility-service's visit-chat endpoint (Stage 3).

Built once, on first use, and memoized — mirrors this codebase's read-config-
once-at-process-start convention (config.py's module-level `settings =
Settings()`; check.py's module-level breaker/client). Constructing the
default `raw_bedrock` runtime validates BEDROCK_MODEL_ID/AWS_REGION
immediately (libs/eligibility_agent/bedrock_tool_port.py::
BedrockConverseToolModel raises ProviderNotConfiguredError at __init__),
which is exactly what happens in this repo's own default configuration
(BEDROCK_MODEL_ID=changeme — no live Bedrock credential is available here by
design). That failure is caught here ONCE and remembered, so every
/visits/*/messages call after the first gets the same safe "assistant
unavailable" reply instead of repeatedly retrying a construction that would
fail identically every time (env vars don't change without a restart, same
assumption every Settings-at-import-time class in this repo already makes).
"""
import logging
from datetime import datetime, timezone
from typing import Iterator, Optional

import redis as redis_lib

from config import settings
from libs.metrics import ai as ai_metrics
from libs.tracing.spans import new_correlation_id, safe_span
from libs.eligibility_agent import (
    AgentRuntime,
    RedisVisitMemory,
    TerminationReason,
    VisitContext,
    VisitMemoryPort,
    VisitStreamEvent,
    VisitTurnResult,
    build_agent_runtime,
)

log = logging.getLogger(__name__)

UNAVAILABLE_REPLY = "The eligibility assistant isn't available right now. Please check eligibility manually."

_runtime: Optional[AgentRuntime] = None
_runtime_build_failed = False
_memory: Optional[VisitMemoryPort] = None
_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def get_visit_memory() -> VisitMemoryPort:
    global _memory
    if _memory is None:
        _memory = RedisVisitMemory(_redis())
    return _memory


def get_agent_runtime() -> Optional[AgentRuntime]:
    """Memoized AgentRuntime, or None if it could not be built (unconfigured
    provider, unknown ELIGIBILITY_AGENT_RUNTIME). Never raises — a
    construction failure is logged (TYPE only) once and remembered."""
    global _runtime, _runtime_build_failed
    if _runtime is not None:
        return _runtime
    if _runtime_build_failed:
        return None
    try:
        _runtime = build_agent_runtime(memory=get_visit_memory())
        return _runtime
    except Exception as exc:
        log.warning("eligibility agent runtime unavailable (error_type=%s)", type(exc).__name__)
        _runtime_build_failed = True
        return None


# Bounded metrics label for this surface.
_METRICS_USE_CASE = "eligibility_agent_chat"
_TRACER_NAME = "eligibility_agent"


def _record_run(termination_reason) -> None:
    """One completed eligibility assistant turn.

    `provenance_label` is deliberately `not_applicable`: unlike the summary
    agent and Policy Navigator, this surface has no real/fixture/fallback
    distinction of its own, and borrowing one would report a provenance the
    code never actually determines.
    """
    ai_metrics.record_agent_run(
        use_case=_METRICS_USE_CASE,
        provenance_label=ai_metrics.NOT_APPLICABLE,
        termination_reason=getattr(termination_reason, "value", termination_reason),
    )


def handle_visit_message(visit_id: str, message: str) -> VisitTurnResult:
    """Safe entry point for the visit-chat endpoint: degrades to a safe
    reply if the runtime isn't available, mirroring AgentRuntime.
    handle_message's own "never raise for a provider/tool failure" contract
    — a missing/misconfigured runtime is just another provider failure from
    the caller's point of view.

    W10 metrics Stage 3: also the entry/terminal-outcome span for this
    turn — provider_call/agent_decision/tool_call spans inside
    RawBedrockAgentRuntime nest under it via OTel's own ambient context.
    `correlation_id` is generated fresh here purely to tie this turn's
    spans together (and, optionally, to a Loki log line); it is never
    persisted and is unrelated to `visit_id`, which is never used as a
    span attribute — a visit is a specific patient's specific encounter,
    the same identifier class agent_provenance.FORBIDDEN_KEYS already
    excludes from the durable trace.
    """
    correlation_id = new_correlation_id()
    with safe_span(_TRACER_NAME, "eligibility_agent.turn", {"correlation_id": correlation_id}) as span:
        runtime = get_agent_runtime()
        if runtime is None:
            _record_run(TerminationReason.PROVIDER_ERROR)
            span.set_attribute("termination_reason", TerminationReason.PROVIDER_ERROR.value)
            return VisitTurnResult(
                visit_id=visit_id,
                reply=UNAVAILABLE_REPLY,
                tool_called=False,
                eligibility_status=None,
                termination_reason=TerminationReason.PROVIDER_ERROR,
                turns_used=0,
            )
        result = runtime.handle_message(visit_id, message)
        _record_run(result.termination_reason)
        span.set_attribute("termination_reason", getattr(result.termination_reason, "value",
                                                          result.termination_reason))
        span.set_attribute("tool_called", result.tool_called)
        return result


_UNSET = object()  # distinguishes "coverage_on_file not passed" from "explicitly None"


# VisitStreamEvent's own contract: "Exactly one of 'done'/'error' ends a
# stream." Those are therefore the two kinds that mean a turn actually
# terminated.
_TERMINAL_STREAM_KINDS = frozenset({"done", "error"})


def _record_terminal_run(events: Iterator[VisitStreamEvent]) -> Iterator[VisitStreamEvent]:
    """Count exactly one agent run per streamed turn, at its terminal event.

    Wrapping the whole generator is what makes "exactly once" true for every
    branch below — unavailable runtime, non-streaming fallback, and the real
    streaming runtime all end with a single done/error event, and none of
    them has to remember to record for itself.

    A stream that ends WITHOUT a terminal event records nothing, deliberately.
    Per VisitStreamEvent's contract a client that sees neither done nor error
    "was disconnected ... not answered", so there is no termination reason to
    report and inventing one would misreport a disconnect as an outcome.
    """
    recorded = False
    for event in events:
        if not recorded and event.kind in _TERMINAL_STREAM_KINDS:
            _record_run(event.termination_reason)
            recorded = True
        yield event


def stream_visit_message(visit_id: str, message: str) -> Iterator[VisitStreamEvent]:
    """Streaming counterpart to handle_visit_message, with run accounting.

    The frontend posts to the streaming route, so this — not
    handle_visit_message — is the path most live eligibility turns take, and
    it must reach `agent_runs_total` or the metric undercounts real usage.

    W10 metrics Stage 3: also the entry/terminal-outcome span for the whole
    streamed turn, same correlation_id/visit_id reasoning as
    handle_visit_message. The span stays open for the WHOLE stream (first
    event to the terminal one) — `safe_span`'s own GeneratorExit handling
    closes it cleanly if the caller disconnects before a terminal event,
    the same way `_record_terminal_run` already records no run for that case.
    """
    correlation_id = new_correlation_id()
    with safe_span(_TRACER_NAME, "eligibility_agent.turn", {"correlation_id": correlation_id}) as span:
        for event in _record_terminal_run(_stream_visit_message(visit_id, message)):
            if event.kind in _TERMINAL_STREAM_KINDS:
                span.set_attribute("termination_reason",
                                   getattr(event.termination_reason, "value", event.termination_reason))
                span.set_attribute("tool_called", bool(event.tool_called))
            yield event


def _stream_visit_message(visit_id: str, message: str) -> Iterator[VisitStreamEvent]:
    """w-9-2-planner P1b: streaming counterpart to handle_visit_message.
    Degrades the same way — a missing/misconfigured runtime yields one
    "error" event carrying UNAVAILABLE_REPLY, never raises. A runtime that
    doesn't implement handle_message_stream (langchain — a comparison
    spike never wired into a running service) falls back to its own
    blocking handle_message and replays the complete reply as a single
    delta followed by one terminal event; every OTHER caller of this
    function (the streaming endpoint) never needs to know which case it
    got."""
    runtime = get_agent_runtime()
    if runtime is None:
        yield VisitStreamEvent(
            kind="error",
            text=UNAVAILABLE_REPLY,
            tool_called=False,
            eligibility_status=None,
            termination_reason=TerminationReason.PROVIDER_ERROR,
            turns_used=0,
        )
        return

    stream = getattr(runtime, "handle_message_stream", None)
    if stream is None:
        result = runtime.handle_message(visit_id, message)
        is_error = result.termination_reason == TerminationReason.PROVIDER_ERROR
        if result.reply and not is_error:
            yield VisitStreamEvent(kind="delta", text=result.reply)
        yield VisitStreamEvent(
            kind="error" if is_error else "done",
            text=result.reply if is_error else None,
            tool_called=result.tool_called,
            eligibility_status=result.eligibility_status,
            termination_reason=result.termination_reason,
            turns_used=result.turns_used,
        )
        return
    yield from stream(visit_id, message)


def bind_visit_context(
    visit_id: str,
    *,
    patient_id: Optional[int] = None,
    insurance_id: Optional[str] = None,
    coverage_on_file=_UNSET,
) -> None:
    """Seed/update the visit's structured memory with the patient/insurance/
    stored-coverage binding the front desk already has on file, so
    verify_current_eligibility has an insurance_id to check and
    get_coverage_on_file has a stored snapshot to read — without the model
    ever supplying either (see libs/eligibility_agent/eligibility_tool.py's
    anti-smuggling design — the model can never pass its own insurance/
    member/patient id or coverage facts). `coverage_on_file`, when given, is
    the plain dict services/gateway/app.py::proxy_visit_message derives
    server-side from the patient's actual insurance_coverages row
    (payer_name, plan_type, member_id_masked, status, verified_at) — never
    taken from the request as anything the model could see or edit.

    w-9-2-planner P1a review fix (B2-stale-snapshot): the gateway always
    passes `coverage_on_file` explicitly on every call to post_visit_message,
    including an explicit `None` when the patient's coverage on file has been
    removed. That explicit-None must CLEAR any coverage_* fields already
    bound to this visit, not leave them stale — which requires distinguishing
    "coverage_on_file not passed at all" (the omitted-kwarg default, used by
    callers that don't touch coverage) from "explicitly passed as None" (a
    signal to clear it). `_UNSET`, not `None`, is the omitted default.

    A no-op if patient_id/insurance_id are both None AND coverage_on_file was
    never passed. Memory-store failures degrade the same way
    RedisVisitMemory.put always does: silently, logged TYPE-only, never
    raised into the request handler."""
    if patient_id is None and insurance_id is None and coverage_on_file is _UNSET:
        return
    memory = get_visit_memory()
    existing = memory.get(visit_id)
    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if patient_id is not None:
        updates["patient_id"] = patient_id
    if insurance_id is not None:
        updates["insurance_id"] = insurance_id
    if coverage_on_file is not _UNSET:
        updates["coverage_payer_name"] = coverage_on_file.get("payer_name") if coverage_on_file else None
        updates["coverage_plan_type"] = coverage_on_file.get("plan_type") if coverage_on_file else None
        updates["coverage_member_id_masked"] = coverage_on_file.get("member_id_masked") if coverage_on_file else None
        updates["coverage_status"] = coverage_on_file.get("status") if coverage_on_file else None
        updates["coverage_verified_at"] = coverage_on_file.get("verified_at") if coverage_on_file else None
    if existing is not None:
        context = existing.model_copy(update=updates)
    else:
        context = VisitContext(visit_id=visit_id, **updates)
    memory.put(context)
