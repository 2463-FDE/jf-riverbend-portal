"""Pydantic v2 response schemas for eligibility-service (X12 270/271 shaped)."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from contracts import EligibilityStatus


class EligibilityResponse(BaseModel):
    insurance_id: str
    active: bool  # kept for backward compatibility; prefer `status` (3+ states)
    status: EligibilityStatus
    payer: Optional[str] = None
    raw_status: Optional[int] = None
    checked_at: datetime
    stale: bool = False
    error: Optional[str] = None  # exception TYPE name only, never a raw message (PHI-safe)
