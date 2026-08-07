"""ORM models intake-service touches. (Copy-paste per service — no shared lib yet.)"""
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from db import Base


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True)            # sequential, exposed in record URLs
    mrn = Column(Text)                                # not used as a match key
    name = Column(Text, nullable=False)               # legacy/composed; see schemas.Demographics
    first_name = Column(Text)                         # structured (migration 011)
    last_name = Column(Text)                          # structured (migration 011)
    dob = Column(Text)                                # stored as ISO string, not DATE
    ssn = Column(Text)                                # plain text
    gender = Column(Text)
    address = Column(Text)                            # legacy/composed; see schemas.Demographics
    city = Column(Text)                               # structured (migration 011)
    state = Column(Text)                              # structured (migration 011)
    zip_code = Column(Text)                            # structured (migration 011), TEXT to preserve leading zeros / ZIP+4
    phone = Column(Text)
    email = Column(Text)
    notes = Column(Text)
    created_via = Column(Text)                        # self_service | front_desk
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InsuranceCoverage(Base):
    __tablename__ = "insurance_coverages"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    payer_name = Column(Text)
    member_id = Column(Text)
    group_number = Column(Text)
    plan_type = Column(Text)                          # PPO | HMO | Medicaid | Medicare | self_pay
    status = Column(Text, default="unknown")          # active | inactive | unknown
    verified_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Consent(Base):
    __tablename__ = "consents"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    kind = Column(Text)                               # npp_ack | treatment_consent | roi_consent
    signed_at = Column(DateTime(timezone=True), server_default=func.now())


class PatientLink(Base):
    """Non-destructive identity-link audit trail (adr/0004, RIV-160,
    migration 012). See app.py::_find_match_candidates for how rows here
    get created; this table is never used to merge or rewrite any other
    table's patient_id foreign keys."""

    __tablename__ = "patient_links"
    __table_args__ = (
        CheckConstraint("confidence IN ('exact', 'partial')", name="patient_links_confidence_check"),
        CheckConstraint("patient_id <> linked_patient_id", name="patient_links_no_self_link_check"),
    )

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    linked_patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    confidence = Column(Text, nullable=False)         # exact | partial
    basis = Column(Text)                              # coded reason only, never raw PHI values
    confirmed = Column(Boolean, nullable=False, default=False)
    confirmed_by = Column(Text)                       # staff identifier; NULL until confirmed
    confirmed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PatientAccessGrant(Base):
    """Week 4 catch-up (migration 014, PR #23) — the per-(user, patient) access
    grant that records-service's SqlPatientAccessGate authorizes against.
    intake-service writes a row here when a *front-desk* staff member registers
    a patient (an authenticated actor is present), so the registrar can
    immediately open the chart they just created without a separate grant step.
    Self-service intake has no staff actor and creates no grant — those
    patients need an explicit grant before any staff can view them (see
    docs/runbook.md). Keyed on users.id, never username."""

    __tablename__ = "patient_access_grants"
    __table_args__ = (UniqueConstraint("user_id", "patient_id"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    granted_at = Column(DateTime(timezone=True), server_default=func.now())
    revoked_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))
