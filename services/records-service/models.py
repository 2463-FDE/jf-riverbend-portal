"""ORM models records-service touches. (Copy-paste per service — no shared lib yet, ADR 0001.)"""
from sqlalchemy import Column, Integer, Text
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
