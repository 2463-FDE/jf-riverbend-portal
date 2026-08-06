"""Pydantic v2 request/response schemas for intake-service."""
from typing import Any, Optional

from pydantic import BaseModel, model_validator

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


# Round-15 review (2026-08-06): the two consents the intake wizard's
# "Continue" button already refuses to proceed without — see
# frontend/app/intake/page.tsx's consentsOk / c_treatment / c_privacy.
_REQUIRED_CONSENT_KINDS = frozenset({"npp_ack", "treatment_consent"})

# The two required kinds above, plus the two optional ones the wizard's
# consent step actually offers (c_financial / c_comms in the same file).
# roi_consent (models.py's Consent.kind comment) is deliberately excluded —
# that is written by roi-service's own release-of-information flow, not
# collected during intake.
_VALID_CONSENT_KINDS = _REQUIRED_CONSENT_KINDS | frozenset(
    {"financial_agreement", "communications_consent"}
)


class IntakeRequest(BaseModel):
    """demographics/insurance/consents are the original contract.

    Round-15 review (2026-08-06): `consents` used to accept any list,
    including an empty one — the UI already refuses to let a patient
    continue past the consent step without checking treatment+privacy (see
    frontend/app/intake/page.tsx's `consentsOk`), but that is browser button
    state, not a trust boundary. A caller with a valid gateway session, a
    stale client, or a direct internal-token caller could still submit
    `consents: []` (or drop just one required kind) and, since round-13's
    atomic commit, have that incomplete registration persist durably with
    no way to flag it afterward. `_validate_consents` below now enforces the
    same two-required-consent rule server-side, and rejects any kind outside
    `_VALID_CONSENT_KINDS` so a typo'd or made-up consent name fails loudly
    (a 422) instead of silently persisting as an untracked string in the
    `consents` table.

    Round-16 review (2026-08-06): round-15 closed `consents: []` but missed
    that omitting the field entirely still validated — `consents` had a
    default (`["npp_ack", "treatment_consent"]`), so a caller that dropped
    the field altogether still passed validation, and that default got
    persisted by `create_intake` as if the patient had actually signed
    those consents. `consents` is now a required field with no default;
    omitting it is exactly as invalid as sending `[]`.

    Round-10 review (2026-08-05, RIV-160): this used to also accept
    duplicate_override="create_new" + confirmed_by, letting a caller bypass
    an exact SSN+DOB match block and record a "confirmed" patient_links row
    under a name of their own choosing. intake-service has no auth
    dependency and is exposed directly on the host (docker-compose.yml, port
    8071) — same root problem as "link_to_existing" before it (removed in
    round-8): there is no server-derived actor identity anywhere in this
    system (no session/auth propagation to intake-service), so nothing
    stopped ANY caller from both forcing the duplicate through AND forging
    who supposedly approved it. Both fields are removed entirely rather than
    trusted verbatim; an exact match now always blocks with 409, no
    exceptions, from this endpoint. A staff-authenticated path to knowingly
    create a confirmed duplicate is deferred, unbuilt future work — see
    app.py::create_intake and _find_match_candidates.
    """

    demographics: Demographics
    insurance: Optional[Insurance] = None
    # Round-16 review (2026-08-06): this used to default to
    # ["npp_ack", "treatment_consent"] when the field was omitted entirely —
    # round-15 closed consents:[] and one-missing-required, but a caller
    # that dropped the field altogether still validated, and create_intake
    # persisted those defaulted consents as if the patient had actually
    # signed them. No default now: omitting consents is exactly as invalid
    # as sending [] or a payload missing a required kind.
    consents: list[str]

    @model_validator(mode="after")
    def _validate_consents(self) -> "IntakeRequest":
        unknown = sorted(set(self.consents) - _VALID_CONSENT_KINDS)
        if unknown:
            raise ValueError(f"unknown consent kind(s): {', '.join(unknown)}")
        missing = sorted(_REQUIRED_CONSENT_KINDS - set(self.consents))
        if missing:
            raise ValueError(f"missing required consent(s): {', '.join(missing)}")
        return self


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
    # Week 2-3 catch-up (adr/0004/RIV-160): whether this intake partially
    # matched an existing patient (ssn agreed, dob did not) — never blocking,
    # just a flag for staff review. The match is recorded server-side in
    # patient_links (confidence="partial"); the candidate patient_id is
    # deliberately NOT returned here (PR #20 round-8 review: this endpoint
    # has no auth dependency, so returning real patient ids would let an
    # unauthenticated caller enumerate patients via ssn/dob probing).
    possible_duplicate_match: bool = False
