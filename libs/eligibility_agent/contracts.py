"""Shared contracts for the eligibility agent — provider-neutral, framework-
neutral. Both AgentRuntime implementations (raw_bedrock, langchain) speak
this vocabulary, which is what lets one fake-only test suite exercise both.

`EligibilityStatus`/`VisitContext` mirror the same-named contracts in
services/eligibility-service/contracts.py (Stage 1) in shape, not in code —
there is no shared Python package between a service and libs/eligibility_agent
any more than there is between two services (adr/0001's no-shared-library
convention, applied the same way here). Keep the two EligibilityStatus enums'
values in sync if either ever changes.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict


class EligibilityStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"
    PENDING = "pending"
    STALE = "stale"


def parse_as_of(as_of) -> Optional[datetime]:
    """Convert the eligibility tool payload's `as_of` (the eligibility-service
    response's own `checked_at`) into a datetime.

    This is the REAL verification time — for a stale last-known-good fallback
    it is deliberately the original, older check time. A runtime must persist
    THIS, never `now()`, or a stale result would be recorded as freshly
    verified. Returns None when `as_of` is absent (a failed/no-check path) or
    unparseable, so the caller can preserve the prior timestamp instead of
    inventing one.
    """
    if not as_of:
        return None
    if isinstance(as_of, datetime):
        return as_of
    try:
        # `.replace("Z", ...)` for Python versions whose fromisoformat predates
        # native "Z" support; a no-op on already-offset strings.
        return datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class VisitContext(BaseModel):
    """Structured, visit-scoped memory. NOT a chat transcript — this is the
    entire persistence surface for a visit; there is deliberately no field
    here for prior messages, prompts, or model responses."""

    visit_id: str
    patient_id: Optional[int] = None
    insurance_id: Optional[str] = None
    eligibility_status: Optional[EligibilityStatus] = None
    eligibility_checked_at: Optional[datetime] = None
    updated_at: datetime


class TerminationReason(str, Enum):
    ANSWERED = "answered"  # the model produced a final text reply
    MAX_TURNS = "max_turns"  # the bounded tool loop was cut off
    PROVIDER_ERROR = "provider_error"  # the model/provider call failed


class VisitTurnResult(BaseModel):
    visit_id: str
    reply: str
    tool_called: bool = False
    eligibility_status: Optional[EligibilityStatus] = None
    termination_reason: TerminationReason
    turns_used: int


class CheckEligibilityArgs(BaseModel):
    """The check_eligibility tool's argument schema — deliberately EMPTY.

    The model may call check_eligibility with no arguments at all; it always
    checks the insurance already on file for the bound visit. `extra="forbid"`
    means ANY additional key the model supplies (attempting to smuggle in an
    endpoint, credential, member_id, or an arbitrary patient_id) fails
    validation before the tool implementation ever runs — and the
    implementation itself never reads model-supplied identifiers regardless
    (see eligibility_tool.py). Two independent layers, not one.
    """

    model_config = ConfigDict(extra="forbid")


class ToolInvocationResult(BaseModel):
    """`ok=False` only for a rejected dispatch (unknown tool name, malformed
    arguments) — a *handled* downstream failure (eligibility-service
    unreachable) is still ok=True, with payload status="unknown"; the tool
    succeeded at producing a safe answer even though the live check did not.
    """

    ok: bool
    payload: dict
