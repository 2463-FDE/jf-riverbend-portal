"""ORM models the gateway touches. (Copy-paste per service — no shared lib yet.)"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.sql import func

from db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(Text, unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    full_name = Column(Text)
    role = Column(Text, nullable=False, default="staff")   # see config/roles.yaml — "staff" is now a
    # deprecated legacy role kept only for existing/seeded accounts (production-readiness Stage 1
    # item 4); four real least-privilege roles exist (front_desk/clinician/roi_clerk/scheduler) but
    # no account has been migrated onto one yet.
    is_active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # production-readiness Stage 1 item 3 (016_user_mfa.sql): mfa_secret is
    # set on first MFA-required login, but not "confirmed" until
    # mfa_enrolled_at is stamped by a successful /login/mfa verify — see
    # mfa.py and app.py::login/login_mfa.
    mfa_secret = Column(Text)
    mfa_enrolled_at = Column(DateTime(timezone=True))


# --- Stage 2 (feature-readiness): visit-chat authorization -----------------
#
# Minimal read-only mirrors of tables scheduling-service/records-service
# actually own (adr/0001 — no shared service library; every service that
# needs a table defines its own columns-must-match-db/schema.sql copy, same
# as User above and records-service's own PatientAccessGrant). Only the
# columns visit_authorization.py's queries need are declared.


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)


class PatientAccessGrant(Base):
    """See services/records-service/models.py's identical table — this is
    the same patient-ownership fact (Week 4 catch-up, RIV-201), read here to
    gate the visit-chat route the same way records-service gates chart
    reads. `user_id`'s FK resolves against THIS service's own `User` model
    above (declarative metadata is per-service, not shared)."""

    __tablename__ = "patient_access_grants"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    patient_id = Column(Integer, nullable=False)
    granted_at = Column(DateTime(timezone=True), server_default=func.now())
    revoked_at = Column(DateTime(timezone=True))  # NULL = active; set = explicitly revoked
    expires_at = Column(DateTime(timezone=True))  # NULL = never expires


class InsuranceCoverage(Base):
    __tablename__ = "insurance_coverages"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    member_id = Column(Text)
