"""Pydantic v2 request/response schemas for intake-service."""
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

# created_via is documented (models.py, schema.sql) as self_service | front_desk
# only, but the field itself is a plain string a caller controls — see
# Demographics._derive_legacy_fields, which normalizes anything outside this
# set to "unknown" rather than trusting it verbatim (PR #20 review: an
# untrusted created_via value flows straight into an INFO log line next to
# the new patient_id).
_VALID_CREATED_VIA = frozenset({"self_service", "front_desk"})


class Demographics(BaseModel):
    """Patient demographics + contact.

    Additive/transitional (Week 6 UI update): accepts either the legacy
    combined fields (`name`, one-blob `address`) or the structured fields
    (`first_name`/`last_name`, split `address`/`city`/`state`/`zip_code`) —
    or both. Whenever structured input is supplied, `_derive_legacy_fields`
    below composes and overwrites `name`/`address` so every existing reader
    of those two columns (records-service, reports, this service's own
    Patient model) keeps working unchanged. Legacy-only callers are passed
    through as before; their new structured columns stay NULL, since a
    free-text legacy name/address cannot be reliably split back apart.
    """

    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    dob: Optional[str] = None
    ssn: Optional[str] = None
    gender: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None
    created_via: str = "self_service"

    @model_validator(mode="after")
    def _derive_legacy_fields(self) -> "Demographics":
        first = (self.first_name or "").strip()
        last = (self.last_name or "").strip()
        self.first_name = first or None
        self.last_name = last or None

        name = (self.name or "").strip()
        if not name:
            name = f"{first} {last}".strip()
        if not name:
            raise ValueError("must provide name, or first_name and last_name")
        self.name = name

        street = (self.address or "").strip()
        city = (self.city or "").strip()
        state = (self.state or "").strip()
        zip_code = (self.zip_code or "").strip()
        self.city = city or None
        self.state = state or None
        self.zip_code = zip_code or None

        if city or state or zip_code:
            # Structured input: compose the legacy combined `address` value
            # the same way legacy callers already format it ("street, city,
            # ST zip"), skipping any empty parts so optional fields never
            # leave behind stray commas or double spaces.
            region = " ".join(part for part in (state, zip_code) if part)
            self.address = ", ".join(part for part in (street, city, region) if part) or None
        else:
            # Legacy input: `address`, if any, is already the full blob.
            self.address = street or None

        if self.created_via not in _VALID_CREATED_VIA:
            self.created_via = "unknown"

        return self


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
