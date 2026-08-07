"""ORM models records-service touches. (Copy-paste per service — no shared lib yet, ADR 0001.)"""
from sqlalchemy import Column, ForeignKey, Integer, Text, UniqueConstraint
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


class PatientAccessGrant(Base):
    """Week 4 catch-up (migration 014) — the patient-ownership/care-team-
    membership fact RIV-201 identified as missing (docs/analysis/
    RIV-201-patient-records-IDOR.md §6). `username` mirrors
    AuthorizationRequest.actor_id exactly (see services/records-service/
    patient_access_gate.py). Action/purpose are enforced in code, not stored
    per-row here — see that file's module docstring for why."""

    __tablename__ = "patient_access_grants"
    __table_args__ = (UniqueConstraint("username", "patient_id"),)

    id = Column(Integer, primary_key=True)
    # Live-verification fix (intake-service hit this first): `username` is
    # deliberately a plain column, NOT `ForeignKey("users.username")` —
    # records-service's own Base.metadata has no `users` table (it doesn't
    # own that table, per ADR 0001's per-service-metadata split), so
    # SQLAlchemy's unit-of-work cannot resolve a same-metadata FK target for
    # it and raises NoReferencedTableError the moment anything inserts this
    # model through the ORM (this service currently only ever SELECTs it,
    # which doesn't trigger the same resolution — kept plain anyway so an
    # ORM-level write here doesn't quietly reintroduce the same bug later).
    # The real Postgres table (migration 014) already enforces this FK, plus
    # ON DELETE CASCADE, at the database level regardless of what this
    # service's local model declares — patient_id below keeps its FK since
    # `patients` IS defined in this same file/metadata and resolves fine.
    username = Column(Text, nullable=False)
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
