"""ORM models records-service touches. (Copy-paste per service — no shared lib yet, ADR 0001.)"""
from sqlalchemy import JSON, Boolean, Column, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.sql import func

from db import Base


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True)
    mrn = Column(Text)
    name = Column(Text, nullable=False)  # legacy/composed; see intake-service/schemas.py Demographics
    first_name = Column(Text)            # structured (migration 011); NULL for legacy-only patients
    last_name = Column(Text)             # structured (migration 011); NULL for legacy-only patients
    dob = Column(Text)               # AEAD-encrypted (libs/phi_crypto) once dob_key_version is set; ISO-string envelope, not DATE
    ssn = Column(Text)               # AEAD-encrypted (libs/phi_crypto) once ssn_key_version is set
    ssn_digits = Column(Text)        # migration 031: HMAC-SHA256 blind index (libs/phi_crypto), NOT raw digits; read-only here (intake-service writes it)
    gender = Column(Text)
    address = Column(Text)           # legacy/composed; see intake-service/schemas.py Demographics
    city = Column(Text)                  # structured (migration 011); NULL for legacy-only patients
    state = Column(Text)                 # structured (migration 011); NULL for legacy-only patients
    zip_code = Column(Text)              # structured (migration 011); NULL for legacy-only patients
    phone = Column(Text)
    email = Column(Text)
    notes = Column(Text)             # AEAD-encrypted (libs/phi_crypto) once notes_key_version is set
    created_via = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    ssn_key_version = Column(Text)   # migration 031; NULL = not yet migrated (still plaintext); read-only here
    dob_key_version = Column(Text)   # migration 031; NULL = not yet migrated; read-only here
    notes_key_version = Column(Text)  # migration 031; NULL = not yet migrated; read-only here


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
    """Minimal projection of the users table — records-service confirms the
    authenticated principal (users.id) is still active before honoring a
    grant (PR #23 review round 2: a disabled account must not retain chart
    access via an existing grant/session), and reads `role` to enforce the
    signed permission matrix here rather than trusting that the gateway did.

    `role` is read from the database on purpose, never from a request header.
    This service's port is published, so a header would be spoofable by any
    direct caller — and it is also what closes the stale-session gap: the
    gateway writes the role into Redis once at login, so a downgraded or
    disabled account would otherwise keep its old role until the session
    lapsed."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(Text)
    # Added for W9.2 (messaging needs a display name for a sender/patient
    # beyond the bare username) — the column already exists on the real
    # `users` table (migration 021, gateway's own User model); this minimal
    # projection simply hadn't selected it before now.
    full_name = Column(Text)
    role = Column(Text)
    # Added round-1 review (2026-08-23): create_thread must confirm the
    # calling patient's OWN chart matches the path's patient_id, not merely
    # that they hold a grant for it (a grant a clinician also holds would
    # otherwise let them originate a thread the client's UX reserves for the
    # patient). NULL for every staff account; set only for a patient's own
    # account (017), same as gateway's own User model already has it.
    patient_id = Column(Integer)
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
    # Keyed on the stable users.id (PR #23 review). records-service now models a
    # minimal `User` (above), so this FK resolves in this service's own
    # metadata; migration 014 enforces it + ON DELETE CASCADE at the DB level.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    granted_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    revoked_at = Column(TIMESTAMP(timezone=True))  # NULL = active; set = explicitly revoked
    expires_at = Column(TIMESTAMP(timezone=True))  # NULL = never expires


class AuditLog(Base):
    """Append-only at the database boundary since migration 026 (P3,
    w8-planner-2, see that migration for the trigger and why not a REVOKE),
    and tamper-evident since migration 027 (PR #86): `chain_position`/
    `prev_chain_hash`/`chain_hash` form a hash chain, linked and verified by
    `chain_position` (not `id`) and computed by a `BEFORE INSERT` trigger —
    see that migration for the full design and db/migrations/scripts/
    verify_audit_chain.py for the verifier. Detects a row whose own content
    changed or one spliced/removed from the middle of the chain; does NOT
    detect truncation at the tail without an externally stored checkpoint
    this repo does not implement (027's own comment states this precisely).
    Stage 3's `/patients/{id}/view` route is the first writer of this table
    in the codebase (see docs/handover/auditor-questionnaire.md and
    roi-service/app.py's comment on the same gap); a real append-only
    per-patient disclosure-accounting store remains unbuilt, documented
    future work.

    No `deleted_at`: migration 026 dropped it — nothing in this codebase
    ever set or filtered on it, and the append-only trigger rejects any
    UPDATE that would set it now anyway."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor = Column(Text)
    message = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class PatientSummaryReview(Base):
    """The clinician gate over content the summariser refused to show (S3).

    This is an authorization input, not a workflow log. A patient sees refused
    content only when an `approved` row exists for it — no row, `pending`, and
    `rejected` all mean not visible. See migration 018 for the constraints that
    hold that shape at the database level.
    """

    __tablename__ = "patient_summary_reviews"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    record_id = Column(Integer, nullable=False)
    state = Column(Text, nullable=False, default="pending")
    reason = Column(Text)
    decided_by = Column(Integer)
    decided_at = Column(TIMESTAMP(timezone=True))
    decision_note = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class AgentDraftProvenance(Base):
    """A model-generated summary draft: the clinical artifact plus its provenance.

    Migration 020, decided in `adr/0010`. Two things about this table are easy to
    get wrong and both are enforced in the database rather than here:

    1. **`generated_text` is immutable.** A revision is a NEW version, never an
       edit of an approved one — a `BEFORE UPDATE` trigger raises otherwise.
       `status` may still move (`draft` -> `validated` -> `approved`).
    2. **A decided draft must name its decider**, and an undecided one must
       claim neither reviewer nor timestamp (CHECK constraint, mirroring 018).

    ⚠️ `generated_text` is PERSISTED PHI and is AEAD-encrypted (adr/0012
    follow-up, migration 032, `libs/phi_crypto`) — `generated_text_key_version`
    NULL alongside a non-NULL `generated_text` means this row predates that
    migration and is still plaintext, awaiting
    `db/migrations/scripts/encrypt_agent_draft_text.py`'s backfill; every
    row created by current code always has both set together. It must never
    be copied into a trace, a log or a prompt; `libs.agent_provenance`
    raises if anything tries.
    """

    __tablename__ = "agent_draft_provenance"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(Text, nullable=False, default="draft")
    provenance_label = Column(Text, nullable=False)
    # Server-generated (new_correlation_id()), never the caller-supplied
    # X-Request-Id (migration 036, review fix ALC-CORR-COLLISION) — unique
    # so a lifecycle key can never be shared by two drafts.
    correlation_id = Column(Text, nullable=False, unique=True)
    model_id = Column(Text)
    validation_code = Column(Text)
    generated_text = Column(Text, nullable=False)
    generated_text_key_version = Column(Text)  # migration 032; NULL = not yet migrated (still plaintext)
    prompt_version = Column(Text)
    reviewed_by = Column(Integer)
    approved_at = Column(TIMESTAMP(timezone=True))
    rejected_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("patient_id", "version", name="agent_draft_patient_version"),)


class AgentDraftCitation(Base):
    """One citation a draft made, pinned to the SOURCE VERSION it cited.

    Source version matters: an approved document can be superseded, and an
    approval has to stay interpretable against what was actually cited rather
    than against whatever that source says later.
    """

    __tablename__ = "agent_draft_citation"

    id = Column(Integer, primary_key=True)
    draft_id = Column(Integer, ForeignKey("agent_draft_provenance.id"), nullable=False)
    source_id = Column(Text, nullable=False)
    source_version = Column(Text, nullable=False)
    citation_id = Column(Text, nullable=False)
    category = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("draft_id", "citation_id", name="agent_draft_citation_unique"),)


class AgentLifecycleEvent(Base):
    """One stage of one draft's durable lifecycle trace (migration 036,
    W10 Final Stage 4) — see services/records-service/agent_lifecycle.py,
    the only writer/reader.

    Replaces three separate, per-request, in-memory-only TraceRecorder
    instances (generation, review, display) with one persisted stream keyed
    by correlation_id. `sequence` is assigned by a database trigger, never
    supplied here — see migration 036 for why (concurrency-safe, monotonic
    ordering per correlation_id). `attributes` only ever holds whatever
    `libs.agent_provenance.assert_safe` already allowed through: never a
    patient/user id, an actor name, a prompt, a response, draft/retrieved
    text, a credential, or a raw provider error.
    """

    __tablename__ = "agent_lifecycle_events"

    id = Column(Integer, primary_key=True)
    correlation_id = Column(Text, nullable=False)
    # default=0 is a client-side placeholder ONLY — real Postgres's BEFORE
    # INSERT trigger (migration 036) unconditionally overwrites it with the
    # correctly computed value regardless of what's sent. It exists so a
    # dialect with no trigger support (SQLite, in unit tests) still
    # satisfies NOT NULL; those tests never depend on the real value.
    sequence = Column(Integer, nullable=False, default=0)
    stage = Column(Text, nullable=False)
    # Real JSONB in Postgres; generic JSON elsewhere (SQLite in unit tests
    # — this table's real DDL, including the Postgres-only trigger
    # functions, is only ever exercised against a real Postgres in
    # integration tests).
    attributes = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, server_default="{}")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("correlation_id", "sequence", name="agent_lifecycle_events_correlation_sequence_unique"),
        # Review fix ALC-DISPLAY-REPEAT: at most one display row per
        # correlation_id — see migration 036 for the full rationale. The
        # WHERE clause is portable (both dialects support partial indexes),
        # so SQLite unit tests enforce the same invariant as real Postgres.
        Index(
            "agent_lifecycle_events_one_display_per_correlation", "correlation_id",
            unique=True,
            postgresql_where=text("stage = 'display'"),
            sqlite_where=text("stage = 'display'"),
        ),
    )


class MessageThread(Base):
    """A durable conversation between one patient and their authorized care
    team (migration 022, W9.2) — deliberately NOT the eligibility chat's
    transient in-memory conversation, which has no human transcript at all.

    Authorization is the same mechanism as chart access: `patient_id` here is
    checked against `patient_access_grants`, the identical table and query
    every other patient-scoped route in this service already uses (see
    app.py's messaging routes, which reuse `_authorize_or_deny`)."""

    __tablename__ = "message_threads"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    subject = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="open")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class ThreadMessage(Base):
    """One message. Sender and body are immutable once written — there is no
    UPDATE route on this table at the application layer, and there must not
    be one: a message already read by its recipient cannot un-say itself."""

    __tablename__ = "thread_messages"
    # Scoped to thread_id, not just sender (round-1 review, MSG-002) — see
    # migration 022's own comment on why a sender-only key let a replay
    # against a different thread return the wrong thread's message.
    __table_args__ = (
        UniqueConstraint(
            "sender_user_id", "thread_id", "idempotency_key",
            name="thread_messages_sender_thread_idem_key",
        ),
    )

    id = Column(Integer, primary_key=True)
    thread_id = Column(Integer, ForeignKey("message_threads.id", ondelete="CASCADE"), nullable=False)
    sender_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    idempotency_key = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class ThreadReadState(Base):
    """One row per (user, thread): the last message that user has seen.
    Unread state is derived from this, not stored as a count — a count would
    drift the moment a message is deleted or a read is recorded out of order,
    and nothing here needs either to happen."""

    __tablename__ = "thread_read_state"

    thread_id = Column(Integer, ForeignKey("message_threads.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_read_message_id = Column(Integer, ForeignKey("thread_messages.id"))
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

