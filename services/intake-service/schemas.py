"""Pydantic v2 request/response schemas for intake-service."""
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class Demographics(BaseModel):
    name: str
    dob: Optional[str] = None
    ssn: Optional[str] = None
    gender: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None
    created_via: str = "self_service"

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()


class Insurance(BaseModel):
    payer_name: Optional[str] = None
    member_id: Optional[str] = None
    group_number: Optional[str] = None
    plan_type: Optional[str] = None


class IntakeRequest(BaseModel):
    demographics: Demographics
    insurance: Optional[Insurance] = None
    consents: list[str] = Field(default_factory=lambda: ["npp_ack", "treatment_consent"])


class IntakeResponse(BaseModel):
    patient_id: int
    elapsed_seconds: float
    # Kept for backward compatibility with any existing caller that reads
    # this dict directly. Stage 3: when insurance is present it now describes
    # the async job's pending/degraded state rather than a completed check —
    # see eligibility_status/eligibility_job_id for the async-aware shape.
    eligibility: Optional[dict[str, Any]] = None
    eligibility_status: Optional[str] = None
    eligibility_job_id: Optional[str] = None
