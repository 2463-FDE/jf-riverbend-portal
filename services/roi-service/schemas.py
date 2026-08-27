"""Pydantic v2 request/response schemas for roi-service."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RoiRequestCreate(BaseModel):
    patient_id: int = Field(..., gt=0)
    requested_by: str = Field(..., min_length=1)
    recipient: str = Field(..., min_length=1)
    recipient_type: str = Field(..., min_length=1)  # self | provider | attorney | payer
    purpose: str | None = None
    date_range_start: str | None = None
    date_range_end: str | None = None


class RoiRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    requested_by: str | None = None
    recipient: str | None = None
    recipient_type: str | None = None
    purpose: str | None = None
    date_range_start: str | None = None
    date_range_end: str | None = None
    status: str
    authorization_reference: str | None = None
    authorization_signed_at: datetime | None = None
    authorization_signed_by: str | None = None
    created_at: datetime | None = None


class FulfillAuthorization(BaseModel):
    """Proof of the signed 45 CFR 164.508 authorization on file, supplied by
    the caller at the moment of release — see app.py::fulfill_roi_request
    for why this is required, not optional. `authorization_reference` is a
    pointer to wherever the actual signed document lives (e.g. a filed
    document ID); this service does not store the document itself."""

    authorization_reference: str = Field(..., min_length=1)
    authorization_signed_at: datetime
    authorization_signed_by: str = Field(..., min_length=1)
    purpose: str | None = None  # falls back to the roi_request's own purpose if omitted


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    encounter_id: int
    patient_id: int
    kind: str | None = None
    title: str | None = None
    body: str | None = None
    status: str | None = None


class FulfillResult(BaseModel):
    request_id: int
    patient_id: int
    status: str
    disclosure_id: int
    records: list[RecordOut]


class DisclosureRecords(BaseModel):
    patient_id: int
    records: list[RecordOut]


class DisclosureAccountingEntry(BaseModel):
    """One line of a 45 CFR 164.528 accounting of disclosures — the exact
    shape docs/handover/auditor-questionnaire.md's Q7 asks for: to whom,
    when, under what authorization, for what purpose."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    roi_request_id: int | None = None
    disclosed_to: str | None = None
    disclosed_at: datetime
    purpose: str | None = None
    authorization_reference: str | None = None


class DisclosureAccounting(BaseModel):
    patient_id: int
    disclosures: list[DisclosureAccountingEntry]
