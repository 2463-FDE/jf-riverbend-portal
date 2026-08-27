"""ORM models roi-service touches. (Copy-paste per service — no shared lib yet, ADR 0001.)"""
from sqlalchemy import Column, Integer, Text
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
    # Set at fulfillment (029, w8-planner-2) — see app.py::fulfill_roi_request.
    # Required non-empty before a fulfillment is accepted; closes the 164.508
    # leg of the module's own DEBT D12 marker.
    authorization_reference = Column(Text)
    authorization_signed_at = Column(TIMESTAMP(timezone=True))
    authorization_signed_by = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    # NOTE: no restriction tracking (164.522) — deliberately still out of scope


class Disclosure(Base):
    __tablename__ = "disclosures"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    roi_request_id = Column(Integer)
    disclosed_to = Column(Text)
    disclosed_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    # Own copy, not just a roi_request_id join — see schema.sql's comment on
    # why (029, w8-planner-2): a 164.528 accounting must describe what was
    # true at the moment of disclosure, not whatever the request row says now.
    authorization_reference = Column(Text)
    purpose = Column(Text)
    # NOTE: no restriction tracking (164.522) — deliberately still out of scope
