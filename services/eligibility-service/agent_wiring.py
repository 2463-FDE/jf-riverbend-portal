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
from typing import Optional

import redis as redis_lib

from config import settings
from libs.eligibility_agent import (
    AgentRuntime,
    RedisVisitMemory,
    TerminationReason,
    VisitContext,
    VisitMemoryPort,
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


def handle_visit_message(visit_id: str, message: str) -> VisitTurnResult:
    """Safe entry point for the visit-chat endpoint: degrades to a safe
    reply if the runtime isn't available, mirroring AgentRuntime.
    handle_message's own "never raise for a provider/tool failure" contract
    — a missing/misconfigured runtime is just another provider failure from
    the caller's point of view."""
    runtime = get_agent_runtime()
    if runtime is None:
        return VisitTurnResult(
            visit_id=visit_id,
            reply=UNAVAILABLE_REPLY,
            tool_called=False,
            eligibility_status=None,
            termination_reason=TerminationReason.PROVIDER_ERROR,
            turns_used=0,
        )
    return runtime.handle_message(visit_id, message)


def bind_visit_context(
    visit_id: str, *, patient_id: Optional[int] = None, insurance_id: Optional[str] = None
) -> None:
    """Seed/update the visit's structured memory with the patient/insurance
    binding the front desk already has on file, so check_eligibility has an
    insurance_id to check without the model ever supplying one (see
    libs/eligibility_agent/eligibility_tool.py's anti-smuggling design — the
    model can never pass its own insurance/member/patient id). A no-op if
    neither field is given. Memory-store failures degrade the same way
    RedisVisitMemory.put always does: silently, logged TYPE-only, never
    raised into the request handler."""
    if patient_id is None and insurance_id is None:
        return
    memory = get_visit_memory()
    existing = memory.get(visit_id)
    updates: dict = {"updated_at": datetime.now(timezone.utc)}
    if patient_id is not None:
        updates["patient_id"] = patient_id
    if insurance_id is not None:
        updates["insurance_id"] = insurance_id
    if existing is not None:
        context = existing.model_copy(update=updates)
    else:
        context = VisitContext(visit_id=visit_id, **updates)
    memory.put(context)
