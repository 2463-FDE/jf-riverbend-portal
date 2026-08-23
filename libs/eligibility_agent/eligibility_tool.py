"""The two tools exposed to either AgentRuntime — the sole way an agent
touches eligibility data.

w-9-2-planner P1a: this used to expose a single check_eligibility tool that
always attempted a live payer call, even though authoritative stored
coverage (payer, plan, masked member id, status, last verified time) is
already sitting in the visit's bound context — so the assistant could never
answer "what's on file" without also triggering a doomed call to the
placeholder payer, and a failed/simulated live attempt read exactly like a
missing record rather than a distinct, honest outcome. Split in two:

  * GetCoverageOnFileTool (get_coverage_on_file) — reads the STORED snapshot
    off VisitContext directly. No network call, ever. Cannot represent a
    payment guarantee or a fresh verification.
  * VerifyCurrentEligibilityTool (verify_current_eligibility) — attempts a
    NEW check, exactly as before, but now distinguishes three outcomes
    ("verified", "simulated", "unavailable") instead of collapsing every
    non-active/inactive case into a bare status. A new attempt never
    silently overwrites stored data with an invented "active" — see
    runtimes/raw_bedrock.py and runtimes/langchain_runtime.py, which persist
    eligibility_status/eligibility_checked_at only for a "verified" outcome.

VerifyCurrentEligibilityTool reuses Stage 1 by calling eligibility-service's
own /eligibility HTTP endpoint — exactly how every other caller in this
codebase (intake-service, gateway) already reaches it — rather than
importing services/eligibility-service/check.py directly. Two reasons:
  1. That module lives inside services/eligibility-service/, not a shared
     libs/ package (adr/0001, and the same Docker/build-context reasoning
     Stage 1 already applied to itself to avoid a premature CI/Docker break).
  2. check.py's circuit breaker and cache are process-local module-level
     singletons. Importing the function directly from a different process
     would give the caller its OWN, unshared breaker/cache state instead of
     the real one guarding production payer traffic — the HTTP endpoint gets
     the actual, shared resilience behavior for free, and is the only way to
     get it from outside eligibility-service's own process.

Both tools are bound to exactly one VisitContext at construction time. Their
JSON-schema-facing signatures (the TOOL_SPECs) take NO arguments —
insurance_id/patient_id/coverage data are never read from model-supplied
input, so a hallucinating or adversarial model cannot smuggle in a different
member ID or patient ID. NoToolArguments (zero fields, extra="forbid") is a
second, independent layer of defense on top of that: even if the model sends
extra keys, they're rejected before invoke() runs, and invoke() itself never
reads them regardless.
"""
import os
from typing import Optional

import httpx
from pydantic import ValidationError

from libs.safe_logging import get_safe_logger

from .contracts import EligibilityStatus, NoToolArguments, ToolInvocationResult, VisitContext

log = get_safe_logger(__name__)

VERIFY_TOOL_NAME = "verify_current_eligibility"
COVERAGE_TOOL_NAME = "get_coverage_on_file"

VERIFY_TOOL_SPEC = {
    "name": VERIFY_TOOL_NAME,
    "description": (
        "Attempt a NEW payer eligibility check for this visit's patient. Takes "
        "no arguments — it always checks the insurance already on file for "
        "this visit. Distinct from get_coverage_on_file: this may contact the "
        "payer (or, in a synthetic training environment, explicitly not), "
        "while get_coverage_on_file only reads the stored record."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}

COVERAGE_TOOL_SPEC = {
    "name": COVERAGE_TOOL_NAME,
    "description": (
        "Look up this visit's patient's insurance coverage already on file — "
        "payer, plan, masked member id, stored status, and when it was last "
        "verified. Takes no arguments. This is the record on file; it never "
        "contacts the payer or attempts a new verification."
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
    def __init__(
        self,
        *,
        eligibility_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        internal_service_token: Optional[str] = None,
        payer_configured: Optional[bool] = None,
    ):
        self.eligibility_url = eligibility_url or os.getenv("ELIGIBILITY_URL", "http://eligibility-service:8072")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.getenv("ELIGIBILITY_TOOL_TIMEOUT_SECONDS", "5"))
        )
        # Branch 7: eligibility-service verifies its callers now, and this tool
        # is one of them. Without the token the call 401s, the except below
        # swallows it, and the assistant reports UNKNOWN coverage — a wrong
        # answer that looks like a legitimate one, which is worse than an
        # error. Read from the environment like the URL above so no caller has
        # to remember to pass it.
        self.internal_service_token = (
            internal_service_token
            if internal_service_token is not None
            else os.getenv("INTERNAL_SERVICE_TOKEN", "")
        )
        # w-9-2-planner P1a: this repo's own PAYER_API_KEY is permanently
        # unset (no real payer, per ADR/README) — attempting the live call
        # anyway used to burn the full retry/backoff/circuit-breaker budget
        # against a placeholder domain just to arrive at an "unknown" that
        # reads like a failed real check. Mirrors gateway's own
        # verify_patient_coverage: check locally, first, and declare the
        # honest "simulated" outcome without ever calling downstream.
        self.payer_configured = (
            payer_configured if payer_configured is not None else bool(os.getenv("PAYER_API_KEY", ""))
        )


class GetCoverageOnFileTool:
    """Pure read of the stored coverage snapshot bound to this visit's
    context — see services/gateway/app.py::proxy_visit_message, the only
    place that snapshot is derived (server-side, from the patient's actual
    insurance_coverages row). No network call, ever; nothing here can be
    mistaken for a payment guarantee or a fresh verification."""

    name = COVERAGE_TOOL_NAME

    def __init__(self, context: VisitContext):
        self._context = context

    def invoke(self, raw_arguments: dict) -> ToolInvocationResult:
        try:
            NoToolArguments.model_validate(raw_arguments or {})
        except ValidationError:
            return ToolInvocationResult(ok=False, payload={"error": "invalid_arguments"})

        ctx = self._context
        if not ctx.coverage_payer_name and not ctx.coverage_status:
            return ToolInvocationResult(
                ok=True,
                payload={"has_coverage_on_file": False, "note": "No coverage on file for this visit."},
            )

        as_of = ctx.coverage_verified_at.isoformat() if ctx.coverage_verified_at else None
        status = ctx.coverage_status or "unknown"
        note = f"Coverage on file: {status}" + (f" as of {as_of}." if as_of else " (verification time not on file).")
        return ToolInvocationResult(
            ok=True,
            payload={
                "has_coverage_on_file": True,
                "payer_name": ctx.coverage_payer_name,
                "plan_type": ctx.coverage_plan_type,
                "member_id_masked": ctx.coverage_member_id_masked,
                "status": status,
                "verified_at": as_of,
                "note": note,
            },
        )


class VerifyCurrentEligibilityTool:
    name = VERIFY_TOOL_NAME

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
            NoToolArguments.model_validate(raw_arguments or {})
        except ValidationError:
            return ToolInvocationResult(ok=False, payload={"error": "invalid_arguments"})

        ctx = self._context
        stored_status = ctx.coverage_status
        stored_as_of = ctx.coverage_verified_at.isoformat() if ctx.coverage_verified_at else None

        if not ctx.insurance_id:
            return ToolInvocationResult(
                ok=True,
                payload={
                    "outcome": "unavailable",
                    "status": stored_status or EligibilityStatus.UNKNOWN.value,
                    "as_of": stored_as_of,
                    "note": "No insurance member id on file to verify.",
                },
            )

        if not self._config.payer_configured:
            return ToolInvocationResult(
                ok=True,
                payload={
                    "outcome": "simulated",
                    "status": stored_status or EligibilityStatus.UNKNOWN.value,
                    "as_of": stored_as_of,
                    "note": (
                        "Synthetic training environment — no real payer contacted. "
                        f"Stored record remains {stored_status or 'unknown'}."
                    ),
                },
            )

        error_type = None
        status = EligibilityStatus.UNKNOWN
        checked_at = None
        try:
            with httpx.Client(transport=self._transport, timeout=self._config.timeout_seconds) as client:
                resp = client.get(
                    f"{self._config.eligibility_url}/eligibility",
                    params={"insurance_id": ctx.insurance_id},
                    headers={"X-Internal-Token": self._config.internal_service_token},
                )
            data = resp.json()
            status = EligibilityStatus(data.get("status", "unknown"))
            checked_at = data.get("checked_at")
            error_type = data.get("error")
        except Exception as exc:
            # Never log the insurance_id or a raw exception message — the
            # error TYPE is enough for operational triage.
            log.warning("verify_current_eligibility tool call failed (error_type=%s)", type(exc).__name__)
            error_type = type(exc).__name__

        if error_type:
            return ToolInvocationResult(
                ok=True,
                payload={
                    "outcome": "unavailable",
                    "status": stored_status or status.value,
                    "as_of": stored_as_of,
                    "note": f"Verification unavailable right now; stored record remains {stored_status or 'unknown'}.",
                },
            )

        return ToolInvocationResult(
            ok=True,
            payload={"outcome": "verified", "status": status.value, "as_of": checked_at, "note": _STATUS_NOTES[status]},
        )
