"""The check_eligibility tool — the ONLY tool exposed to either AgentRuntime,
and the sole way an agent touches Stage 1's resilient eligibility path.

Reuses Stage 1 by calling eligibility-service's own /eligibility HTTP
endpoint — exactly how every other caller in this codebase (intake-service,
gateway) already reaches it — rather than importing services/eligibility-
service/check.py directly. Two reasons:
  1. That module lives inside services/eligibility-service/, not a shared
     libs/ package (adr/0001, and the same Docker/build-context reasoning
     Stage 1 already applied to itself to avoid a premature CI/Docker break).
  2. check.py's circuit breaker and cache are process-local module-level
     singletons. Importing the function directly from a different process
     would give the caller its OWN, unshared breaker/cache state instead of
     the real one guarding production payer traffic — the HTTP endpoint gets
     the actual, shared resilience behavior for free, and is the only way to
     get it from outside eligibility-service's own process.

The tool is bound to exactly one VisitContext at construction time. Its
JSON-schema-facing signature (TOOL_SPEC) takes NO arguments — insurance_id is
never read from model-supplied input, so a hallucinating or adversarial model
cannot smuggle in a different member ID or patient ID. CheckEligibilityArgs
(zero fields, extra="forbid") is a second, independent layer of defense on
top of that: even if the model sends extra keys, they're rejected before
invoke() runs, and invoke() itself never reads them regardless.
"""
import os
from typing import Optional

import httpx
from pydantic import ValidationError

from libs.safe_logging import get_safe_logger

from .contracts import CheckEligibilityArgs, EligibilityStatus, ToolInvocationResult, VisitContext

log = get_safe_logger(__name__)

TOOL_NAME = "check_eligibility"

TOOL_SPEC = {
    "name": TOOL_NAME,
    "description": (
        "Check this visit's patient's current insurance eligibility. Takes no "
        "arguments — it always checks the insurance already on file for this visit."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

_STATUS_NOTES = {
    EligibilityStatus.ACTIVE: "Coverage is currently active.",
    EligibilityStatus.INACTIVE: "Coverage is currently inactive.",
    EligibilityStatus.UNKNOWN: "Coverage could not be verified right now. Try again shortly or check manually.",
    EligibilityStatus.STALE: (
        "Coverage could not be re-verified just now; showing the last known result, which may be outdated."
    ),
    EligibilityStatus.PENDING: "Coverage verification is still in progress.",
}


class EligibilityToolConfig:
    def __init__(self, *, eligibility_url: Optional[str] = None, timeout_seconds: Optional[float] = None):
        self.eligibility_url = eligibility_url or os.getenv("ELIGIBILITY_URL", "http://eligibility-service:8072")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.getenv("ELIGIBILITY_TOOL_TIMEOUT_SECONDS", "5"))
        )


class CheckEligibilityTool:
    name = TOOL_NAME

    def __init__(
        self,
        context: VisitContext,
        *,
        config: Optional[EligibilityToolConfig] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self._context = context
        self._config = config or EligibilityToolConfig()
        self._transport = transport

    def invoke(self, raw_arguments: dict) -> ToolInvocationResult:
        try:
            CheckEligibilityArgs.model_validate(raw_arguments or {})
        except ValidationError:
            return ToolInvocationResult(ok=False, payload={"error": "invalid_arguments"})

        if not self._context.insurance_id:
            return ToolInvocationResult(
                ok=True,
                payload={
                    "status": EligibilityStatus.UNKNOWN.value,
                    "as_of": None,
                    "note": "No insurance on file for this visit.",
                },
            )

        status = EligibilityStatus.UNKNOWN
        checked_at = None
        try:
            with httpx.Client(transport=self._transport, timeout=self._config.timeout_seconds) as client:
                resp = client.get(
                    f"{self._config.eligibility_url}/eligibility",
                    params={"insurance_id": self._context.insurance_id},
                )
            data = resp.json()
            status = EligibilityStatus(data.get("status", "unknown"))
            checked_at = data.get("checked_at")
        except Exception as exc:
            # Never log the insurance_id or a raw exception message — the
            # error TYPE is enough for operational triage.
            log.warning("check_eligibility tool call failed (error_type=%s)", type(exc).__name__)

        return ToolInvocationResult(
            ok=True,
            payload={"status": status.value, "as_of": checked_at, "note": _STATUS_NOTES[status]},
        )
