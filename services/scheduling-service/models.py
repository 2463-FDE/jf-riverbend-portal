"""ORM models scheduling-service touches. (Copy-paste per service — no shared lib yet.)

Columns mirror db/schema.sql exactly.

Stage 4 (Week 5, RIV-175, migration 013): appointments.slot_id used to have
no UNIQUE constraint at all, which was the double-booking race book.py's own
docstring documented. That's now closed at the database level by a partial
unique index (at most one 'confirmed' appointment per slot_id) plus a
per-patient idempotency_key index — see migration 013 and book.py.
"""
from sqlalchemy import Column, DateTime, Integer, Text
from sqlalchemy.sql import func

from db import Base


class Provider(Base):
    __tablename__ = "providers"

    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    specialty = Column(Text)
    location = Column(Text)


class Slot(Base):
    __tablename__ = "slots"

    id = Column(Integer, primary_key=True)
    provider_id = Column(Integer)  # REFERENCES providers(id) at the DB level
    location = Column(Text)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True))
    status = Column(Text, nullable=False, default="open")  # open | booked (advisory)


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    slot_id = Column(Integer, nullable=False)  # no FK to slots(id) yet
    provider = Column(Text)
    reason = Column(Text)
    location = Column(Text)
    scheduled_for = Column(DateTime(timezone=True))
    status = Column(Text, nullable=False, default="confirmed")  # confirmed | cancelled | cancelled_duplicate | completed
    created_at = Column(DateTime(timezone=True), server_default=func.clock_timestamp())
    # Migration 013 (RIV-175): NULL on every pre-migration row.
    idempotency_key = Column(Text)
    # Set only on a 'cancelled_duplicate' row — points at the appointment it
    # lost the confirmed slot to (migration 013's reconciliation, or book.py's
    # own unique-violation handling going forward).
    reconciled_duplicate_of = Column(Integer)


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True)
    mrn = Column(Text)
    name = Column(Text, nullable=False)
    dob = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
