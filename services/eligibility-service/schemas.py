"""Pydantic v2 request/response schemas for eligibility-service (X12 270/271
shaped, plus the Stage 3 job-lifecycle and visit-chat surfaces)."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from contracts import EligibilityStatus
from jobs import JobStatus


class EligibilityResponse(BaseModel):
    insurance_id: str
    active: bool  # kept for backward compatibility; prefer `status` (3+ states)
    status: EligibilityStatus
    payer: Optional[str] = None
    raw_status: Optional[int] = None
    checked_at: datetime
    stale: bool = False
    error: Optional[str] = None  # exception TYPE name only, never a raw message (PHI-safe)


# --- Stage 3: async eligibility job lifecycle --------------------------------


class CreateEligibilityJobRequest(BaseModel):
    insurance_id: str
    # Caller-supplied idempotency key (e.g. intake-service derives one from
    # its own patient_id/coverage_id) so a retried enqueue call — a network
    # blip on intake-service's side, not a new registration — returns the
    # SAME job instead of triggering a second live payer call. A caller that
    # doesn't supply one gets no idempotency protection for that request.
    idempotency_key: Optional[str] = None


class EligibilityJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    retry_count: int
    max_retries: int
    manual_retry_count: int
    max_manual_retries: int
    result_status: Optional[str] = None
    result_checked_at: Optional[datetime] = None
    error: Optional[str] = None  # exception TYPE name only (PHI-safe)
    created_at: datetime
    updated_at: datetime


# --- Stage 3: visit-scoped assistant turns ------------------------------------


class VisitMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    # Optional binding so the front desk can attach the patient/insurance
    # already on file for this visit before the model ever needs to call
    # check_eligibility — see agent_wiring.py::bind_visit_context. The model
    # itself can never supply/override these (libs/eligibility_agent's
    # CheckEligibilityArgs takes zero fields, extra="forbid").
    patient_id: Optional[int] = None
    insurance_id: Optional[str] = None


class VisitMessageResponse(BaseModel):
    visit_id: str
    reply: str
    tool_called: bool
    eligibility_status: Optional[EligibilityStatus] = None
    termination_reason: str
    turns_used: int
