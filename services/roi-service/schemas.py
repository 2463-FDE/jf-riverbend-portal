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
    authorization_id: int | None = None
    authorization_reference: str | None = None
    authorization_signed_at: datetime | None = None
    authorization_signed_by: str | None = None
    created_at: datetime | None = None


# --- 164.508 authorization lifecycle (030) ----------------------------------- #


class AuthorizationCreate(BaseModel):
    """A submitted authorization, awaiting human review — `status` starts
    'pending' regardless of what's supplied here; nothing in this payload
    can mark itself 'valid'. See app.py::review_authorization."""

    patient_id: int = Field(..., gt=0)
    recipient: str = Field(..., min_length=1)
    purpose: str | None = None
    scope_start: str | None = None
    scope_end: str | None = None
    signature_evidence_reference: str = Field(..., min_length=1)
    signature_evidence_digest: str | None = None
    signed_by: str = Field(..., min_length=1)
    signed_at: datetime
    expires_at: datetime | None = None
    representative_authority: str | None = None


class AuthorizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    recipient: str
    purpose: str | None = None
    scope_start: str | None = None
    scope_end: str | None = None
    signature_evidence_reference: str
    signature_evidence_digest: str | None = None
    signed_by: str
    signed_at: datetime
    expires_at: datetime | None = None
    status: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    revoked_at: datetime | None = None
    representative_authority: str | None = None
    created_at: datetime | None = None


class AuthorizationReview(BaseModel):
    """A human reviewer's decision — the only path an authorization can
    reach 'valid' through. `decision` must be exactly 'valid' or
    'rejected'; anything else (including attempting to set 'pending' or
    'revoked' here) is refused."""

    decision: str = Field(..., pattern="^(valid|rejected)$")
    reviewed_by: str = Field(..., min_length=1)


class AuthorizationRevoke(BaseModel):
    revoked_by: str = Field(..., min_length=1)


# --- fulfillment (030: authorization_id only, no caller-supplied evidence) --- #


class FulfillRequest(BaseModel):
    """Fulfillment now supplies only a reference to a PERSISTED, reviewed
    roi_authorizations row — see app.py::fulfill_roi_request, which loads
    and revalidates it (status/expiry/revocation/patient/recipient/scope)
    itself. A caller cannot assert its own signer/reference/timestamp at
    fulfillment time (030) the way 029's design allowed."""

    authorization_id: int = Field(..., gt=0)


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


class DisclosureAccountingEntry(BaseModel):
    """One internally logged disclosure. NOT a 45 CFR 164.528 accounting of
    disclosures on its own — 164.528(a)(2) exempts disclosures made
    pursuant to a valid 164.508 authorization (which every row behind this
    endpoint is) from that mandatory accounting requirement. See
    models.py::Disclosure for the full explanation of what this is and
    is not a substitute for."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    roi_request_id: int | None = None
    authorization_id: int | None = None
    disclosed_to: str | None = None
    disclosed_at: datetime
    purpose: str | None = None
    authorization_reference: str | None = None


class DisclosureAccounting(BaseModel):
    patient_id: int
    disclosures: list[DisclosureAccountingEntry]


# --- 164.522 disclosure restrictions (030) ------------------------------------ #


class RestrictionCreate(BaseModel):
    """A narrowly scoped restriction — not a consent-management platform.
    `recipient` NULL/omitted blocks every recipient for this patient;
    set, it blocks only that exact recipient string."""

    patient_id: int = Field(..., gt=0)
    recipient: str | None = None
    reason: str | None = None


class RestrictionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patient_id: int
    recipient: str | None = None
    reason: str | None = None
    active: bool
    created_at: datetime | None = None
    revoked_at: datetime | None = None
