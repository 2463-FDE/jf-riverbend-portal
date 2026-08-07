"""ORM models records-service touches. (Copy-paste per service — no shared lib yet, ADR 0001.)"""
from sqlalchemy import Boolean, Column, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.sql import func

from db import Base


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True)
    mrn = Column(Text)
    name = Column(Text, nullable=False)  # legacy/composed; see intake-service/schemas.py Demographics
    first_name = Column(Text)            # structured (migration 011); NULL for legacy-only patients
    last_name = Column(Text)             # structured (migration 011); NULL for legacy-only patients
    dob = Column(Text)               # stored as ISO string, not DATE (legacy)
    ssn = Column(Text)               # plain text (legacy)
    gender = Column(Text)
    address = Column(Text)           # legacy/composed; see intake-service/schemas.py Demographics
    city = Column(Text)                  # structured (migration 011); NULL for legacy-only patients
    state = Column(Text)                 # structured (migration 011); NULL for legacy-only patients
    zip_code = Column(Text)              # structured (migration 011); NULL for legacy-only patients
    phone = Column(Text)
    email = Column(Text)
    notes = Column(Text)
    created_via = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Encounter(Base):
    __tablename__ = "encounters"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    encounter_type = Column(Text)
    provider = Column(Text)
    reason = Column(Text)
    location = Column(Text)
    status = Column(Text)
    summary = Column(Text)
    allergies = Column(Text)
    medications = Column(Text)
    occurred_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


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


class User(Base):
    """Minimal projection of the users table — records-service only needs to
    confirm the authenticated principal (users.id) is still active before
    honoring a grant (PR #23 review round 2: a disabled account must not
    retain chart access via an existing grant/session)."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)


class PatientAccessGrant(Base):
    """Week 4 catch-up (migration 014) — the patient-ownership/care-team-
    membership fact RIV-201 identified as missing (docs/analysis/
    RIV-201-patient-records-IDOR.md §6). `user_id` is the stable authenticated
    principal (users.id) the gateway forwards as X-Actor-Id (PR #23 review
    round 2 — never username). Action/purpose are enforced in code, not stored
    per-row here — see patient_access_gate.py's module docstring for why."""

    __tablename__ = "patient_access_grants"
    __table_args__ = (UniqueConstraint("user_id", "patient_id"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    granted_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    revoked_at = Column(TIMESTAMP(timezone=True))  # NULL = active; set = explicitly revoked
    expires_at = Column(TIMESTAMP(timezone=True))  # NULL = never expires


class AuditLog(Base):
    """Mutable, soft-delete request-dump logging (db/schema.sql) — NOT a
    tamper-evident access trail. Stage 3's `/patients/{id}/view` route is the
    first writer of this table in the codebase (see docs/handover/
    auditor-questionnaire.md and roi-service/app.py's comment on the same
    gap); a real append-only per-patient disclosure-accounting store remains
    unbuilt, documented future work."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor = Column(Text)
    message = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    deleted_at = Column(TIMESTAMP(timezone=True))
