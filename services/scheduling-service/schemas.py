"""Pydantic v2 request/response schemas for scheduling-service."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_id: Optional[int] = None
    provider: Optional[str] = None  # provider name, joined from providers
    location: Optional[str] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    status: str


class SlotListResponse(BaseModel):
    items: List[SlotOut]
    count: int
    limit: int
    offset: int


class AppointmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    slot_id: int
    provider: Optional[str] = None
    reason: Optional[str] = None
    location: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    status: str
    created_at: Optional[datetime] = None


class AppointmentListResponse(BaseModel):
    items: List[AppointmentOut]
    count: int


class BookingRequest(BaseModel):
    patient_id: int = Field(..., gt=0)
    slot_id: int = Field(..., gt=0)
    # Stage 4 (Week 5, RIV-175): required, not optional-with-a-server-default —
    # an idempotency key that silently defaults to "no retry protection" is
    # exactly the kind of gap that turned out to matter in this same PR's
    # intake-consents fix. Scoped per patient (see migration 013's
    # (patient_id, idempotency_key) unique index) — the client generates one
    # UUID per booking attempt and resends the SAME value on any retry of the
    # same click, never a fresh one.
    idempotency_key: str = Field(..., min_length=1, max_length=200)
    provider: Optional[str] = Field(None, max_length=200)
    reason: Optional[str] = Field(None, max_length=2000)
    location: Optional[str] = Field(None, max_length=200)
    scheduled_for: Optional[datetime] = None


class BookingResponse(BaseModel):
    appointment_id: Optional[int] = None
    status: str  # confirmed | slot_taken


class CancelResponse(BaseModel):
    appointment_id: int
    status: str  # cancelled
