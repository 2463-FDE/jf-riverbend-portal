"""Shared-shape Pydantic status contracts for the eligibility path.

There is no shared Python package across services yet (adr/0001), so this
module is self-contained to eligibility-service; intake-service's transport-
failure fallback (app.py::_verify_eligibility) uses the same "unknown" status
string literal. Keep the two in sync if this enum's values ever change.

`VisitContext` and `AuditEvent` are defined now, per the approved Week 3 plan,
so the Stage 2 agent runtime (visit memory) and Stage 3 audit trail build
against a stable shape from day one. Neither is consumed by this service yet.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class EligibilityStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"
    PENDING = "pending"
    STALE = "stale"


class EligibilityResult(BaseModel):
    insurance_id: str
    status: EligibilityStatus
    payer: Optional[str] = None
    raw_status: Optional[int] = None
    checked_at: datetime
    # Populated only when status == STALE: the original (active/inactive)
    # verdict the cache holds, and how old that verdict is.
    cached_status: Optional[EligibilityStatus] = None
    stale_age_seconds: Optional[float] = None
    # Exception TYPE name only when a live check degraded to this result —
    # never a raw exception message. See docs/planning/phi-safe-logging-policy.md.
    error_type: Optional[str] = None


class VisitContext(BaseModel):
    """Structured, front-desk-visit-scoped memory (not raw chat) for the
    Stage 2 agent runtime. Not read or written by this service yet."""

    visit_id: str
    patient_id: Optional[int] = None
    insurance_id: Optional[str] = None
    eligibility_status: Optional[EligibilityStatus] = None
    eligibility_checked_at: Optional[datetime] = None
    updated_at: datetime


class AuditEvent(BaseModel):
    """Generic audit-trail event shape for later use (agent tool calls,
    breaker trips, disclosure accounting). Not emitted by this service yet."""

    event: str
    service: str
    outcome: str
    occurred_at: datetime
    metadata: dict = {}
