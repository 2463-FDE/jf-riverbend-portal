"""ORM models the gateway touches. (Copy-paste per service — no shared lib yet.)"""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.sql import func

from db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(Text, unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    full_name = Column(Text)
    role = Column(Text, nullable=False, default="staff")   # see config/roles.yaml — "staff" is now a
    # deprecated legacy role kept only for existing/seeded accounts; real least-privilege roles are
    # defined there, but no account has been migrated onto one yet (that migration is gated on the
    # client's staff roster).
    # NULL for staff accounts; set for a patient's own account (migration 017).
    # Identity only — it does NOT authorize anything. A patient is scoped to
    # their own chart by holding exactly one patient_access_grants row, through
    # the same gate that scopes staff. Never authorize off this column.
    #
    # No ForeignKey() here on purpose: `patients` is owned by records-service
    # and is not in this service's declarative metadata (ADR 0001, and the same
    # reason Appointment.patient_id below carries none). Migration 017 enforces
    # the constraint where it belongs — in the database.
    patient_id = Column(Integer)
    is_active = Column(Boolean, nullable=False, default=True)
    # WHY the account is inactive (migration 019). Login refuses every inactive
    # account, but the roster migration needs the reason so an unmapped user
    # gets the client's specified message instead of "invalid credentials".
    disabled_reason = Column(Text)
    last_login_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # --- MFA (migration 033) — see that migration's comment for the full
    # rationale on every column here, in particular why mfa_shared_account
    # defaults True and mfa_pilot defaults False. ---
    mfa_secret_ciphertext = Column(Text)
    mfa_secret_key_version = Column(Text)
    mfa_enrolled_at = Column(DateTime(timezone=True))
    mfa_last_totp_step = Column(BigInteger)
    mfa_challenge_epoch = Column(BigInteger, nullable=False, default=0)
    mfa_shared_account = Column(Boolean, nullable=False, default=True)
    mfa_pilot = Column(Boolean, nullable=False, default=False)

    # Monotonic revocation counter (migration 034). require_session compares
    # this against the value stamped into a Redis session at login; bumped by
    # any path that changes is_active/role so a live session dies immediately
    # rather than continuing on stale authorization state.
    security_version = Column(BigInteger, nullable=False, default=0)


class Patient(Base):
    """Minimal read-only mirror of records-service's `patients` table — the
    authoritative source for a patient's name (adr/0001: no shared service
    library, so every service that needs a table defines its own matching
    columns, same as `Appointment`/`PatientAccessGrant` below). Only the two
    columns this service's identity-display and activation paths need."""

    __tablename__ = "patients"

    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)


class PatientInvitation(Base):
    """A clinic-issued code that lets one patient activate one account.

    The code itself is never stored — only its hash, exactly as passwords are.
    An invitation code is a credential for a chart; a readable one in a backup
    or a support screenshot is chart access lying in plain sight.

    `issued_by` is the staff member who created it, kept for accounting: an
    auditor asking "who was given access to this chart, by whom, and when" gets
    an answer from this table without inferring it from timestamps.
    """

    __tablename__ = "patient_invitations"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    code_hash = Column(Text, nullable=False)
    issued_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    issued_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    activated_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    activated_user_id = Column(Integer, ForeignKey("users.id"))


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


class RoiRequest(Base):
    """Minimal read-only mirror of roi-service's own table (008/029/030) —
    only `patient_id` is needed here, to resolve which patient a bare
    `request_id` in a ROI proxy route URL belongs to before checking a
    patient_access_grants row (W10 Final 2 Stage 1 — see
    roi_authorization.py). Same adr/0001 reasoning as Appointment/
    PatientAccessGrant above."""

    __tablename__ = "roi_requests"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)


class RoiAuthorization(Base):
    """Minimal read-only mirror of roi-service's own table (029/030) — same
    reasoning as RoiRequest above, for a bare `authorization_id`."""

    __tablename__ = "roi_authorizations"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)


class InsuranceCoverage(Base):
    __tablename__ = "insurance_coverages"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    payer_name = Column(Text)
    member_id = Column(Text)
    group_number = Column(Text)
    plan_type = Column(Text)
    status = Column(Text)
    verified_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Migration 023 (W9.3) — see that file's own comment. Read/written only
    # by the coverage/eligibility routes below; never serialized to a
    # response — a job id is not something the browser needs or may use as
    # a lookup key (see those routes' own docstrings).
    verification_job_id = Column(Text)


class MfaBackupCode(Base):
    """See migration 033 for the full column rationale. code_hash is
    security.hash_password's PBKDF2-SHA256 output — never queried by
    equality (no index would help; a per-code random salt makes that
    impossible by design), always by scanning a user's active rows and
    calling verify_password against each (at most ten, cheap)."""

    __tablename__ = "mfa_backup_codes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    used_at = Column(DateTime(timezone=True))          # NULL = unused
    invalidated_at = Column(DateTime(timezone=True))   # set by regeneration or a reset


class AuditLog(Base):
    """Mirror of records-service's identical table (ADR 0001 — no shared
    service library; every service that needs a table defines its own
    matching columns). audit_logs itself is append-only and hash-chained at
    the database boundary since migrations 026/027 — this service only ever
    INSERTs, which its runtime credential is granted regardless of which
    service the row's actor came from (migration 028's ALTER DEFAULT
    PRIVILEGES). chain_position/prev_chain_hash/chain_hash are computed by
    a BEFORE INSERT trigger; this model does not set them.

    Gateway is the natural writer for MFA events (enrollment, challenge
    outcomes, backup-code use, regeneration, reset) — it owns login and the
    session/challenge flow those events describe. `message` is metadata
    only, same invariant records-service's own writer already holds:
    identifiers and outcome, never a secret, code, or QR payload."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor = Column(Text)
    message = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
