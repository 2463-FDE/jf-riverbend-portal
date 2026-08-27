"""ORM models roi-service touches. (Copy-paste per service — no shared lib yet, ADR 0001.)"""
from sqlalchemy import Boolean, Column, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.sql import func

from db import Base


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True)
    mrn = Column(Text)
    name = Column(Text, nullable=False)
    dob = Column(Text)
    gender = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Record(Base):
    __tablename__ = "records"

    id = Column(Integer, primary_key=True)
    encounter_id = Column(Integer, nullable=False)
    patient_id = Column(Integer, nullable=False)
    kind = Column(Text)
    title = Column(Text)
    body = Column(Text)
    status = Column(Text)
    reference_range = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class RoiAuthorization(Base):
    """A persisted, human-reviewed 45 CFR 164.508 authorization record (030,
    w8-planner-2) — replaces trusting a fresh signer/reference/timestamp
    payload supplied at the moment of fulfillment (029's original, weaker
    design). fulfill_roi_request loads one of these by id and revalidates
    status/expiry/patient/recipient/scope; it never takes any of these
    fields as caller input at fulfillment time."""

    __tablename__ = "roi_authorizations"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    recipient = Column(Text, nullable=False)
    purpose = Column(Text)
    scope_start = Column(Text)  # NULL = no date-range limit stated
    scope_end = Column(Text)
    signature_evidence_reference = Column(Text, nullable=False)
    signature_evidence_digest = Column(Text)
    signed_by = Column(Text, nullable=False)
    signed_at = Column(TIMESTAMP(timezone=True), nullable=False)
    expires_at = Column(TIMESTAMP(timezone=True))
    status = Column(Text, nullable=False, default="pending")  # pending | valid | rejected | revoked
    reviewed_by = Column(Text)
    reviewed_at = Column(TIMESTAMP(timezone=True))
    revoked_at = Column(TIMESTAMP(timezone=True))
    representative_authority = Column(Text)  # NULL = patient signed for themselves
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class RoiRequest(Base):
    __tablename__ = "roi_requests"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    requested_by = Column(Text)
    recipient = Column(Text)
    recipient_type = Column(Text)        # self | provider | attorney | payer
    purpose = Column(Text)
    date_range_start = Column(Text)
    date_range_end = Column(Text)
    status = Column(Text, nullable=False, default="pending")  # pending | fulfilled | denied
    authorization_id = Column(Integer, ForeignKey("roi_authorizations.id"))
    # Set at fulfillment (029), now copied FROM the loaded/validated
    # roi_authorizations row above — never from caller-supplied input (030).
    authorization_reference = Column(Text)
    authorization_signed_at = Column(TIMESTAMP(timezone=True))
    authorization_signed_by = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Disclosure(Base):
    """Every disclosure this service fulfills is logged here — but this is
    NOT the same thing as a formal 45 CFR 164.528 accounting of
    disclosures. 164.528(a)(2) EXEMPTS disclosures made pursuant to a valid
    164.508 authorization from the mandatory accounting requirement, which
    is exactly what these rows are. A true 164.528 accounting would need to
    track the NON-exempt categories this system does not model at all
    (public health, law enforcement, judicial/administrative proceedings,
    etc.). Do not describe this table, or GET /roi/patients/{id}/accounting,
    as satisfying 164.528 — it is an internal disclosure log with
    164.528-shaped fields, not a substitute for one."""

    __tablename__ = "disclosures"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    roi_request_id = Column(Integer)
    authorization_id = Column(Integer, ForeignKey("roi_authorizations.id"))
    disclosed_to = Column(Text)
    disclosed_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    # Own copy, not just a roi_request_id/authorization_id join — see
    # schema.sql's comment on why: this log must describe what was true AT
    # THE TIME OF DISCLOSURE, unaffected by any later edit to the request
    # or authorization row.
    authorization_reference = Column(Text)
    purpose = Column(Text)


class RoiDisclosureRestriction(Base):
    """A narrowly scoped patient disclosure-restriction record (45 CFR
    164.522) — NOT a general consent-management platform: no category
    taxonomy, no per-record-type scoping, no expiration logic beyond an
    explicit revoke. fulfill_roi_request rechecks active, matching rows
    inside its own transaction and refuses fulfillment outright if one
    exists."""

    __tablename__ = "roi_disclosure_restrictions"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    recipient = Column(Text)  # NULL = blanket restriction; set = blocks this recipient only
    reason = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    revoked_at = Column(TIMESTAMP(timezone=True))
