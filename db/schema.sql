-- Riverbend Patient Portal — consolidated database schema (current state).
-- Postgres 15. PHI is NOT encrypted at rest. `dob`, `ssn` and `notes` below
-- are plain text, and `ssn_digits` is a generated, INDEXED copy of the SSN.
-- This line previously claimed disk-level protection from a managed-database
-- volume. No such deployment exists (docker compose, local `pgdata` volume),
-- and there is no encryption of any kind. See adr/0008 for the recorded risk
-- decision and its remediation plan.
--
-- This file is the flattened "current" schema loaded by docker-entrypoint on a
-- fresh volume. The incremental history lives in db/migrations/*.sql and is kept
-- in sync with this file by hand (see ADR 0001 — no shared library / tooling yet).

-- ---------------------------------------------------------------------------
-- Authentication
-- ---------------------------------------------------------------------------
-- Portal + staff logins. Passwords are PBKDF2 (django-style string).
-- Sessions now carry both an idle and an absolute Redis TTL (see
-- services/gateway/security.py) — they no longer live forever. There is no
-- second factor: TOTP was built, tested, and parked for a complete
-- next-cycle rollout (see config/roles.yaml).
-- `role` still holds 'staff' for every existing account; the real
-- least-privilege roles live in config/roles.yaml and no account has been
-- migrated onto one yet (that migration is gated on the client's roster).
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    -- For a patient-role account, backfilled from patients.name by migration
    -- 021 for any row a pre-fix activation left NULL/blank; a fresh volume
    -- never needs the backfill because activation (and the seed generator)
    -- populate it directly.
    full_name     TEXT,
    -- No DEFAULT (016_users_role_no_default.sql): 'staff' is the deprecated
    -- legacy role and still carries every patient-data permission, so an
    -- INSERT that omitted `role` used to create a full-access account
    -- silently. It must now be set explicitly, or the insert fails.
    role          TEXT NOT NULL,
    -- NULL for staff accounts; set for a patient's own account (017). A
    -- patient is authorized by the SAME grant mechanism as staff — one
    -- patient_access_grants row for their own chart — not a second path.
    -- The foreign key is attached below, after `patients` is created.
    patient_id    INTEGER,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    -- WHY an account is inactive (019). Login refuses every inactive account
    -- identically; this is what lets it tell the roster-migration cases apart
    -- so the client's specified copy can be shown for an unmapped account.
    disabled_reason TEXT,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Patients
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS patients (
    id          SERIAL PRIMARY KEY,            -- sequential, exposed in record URLs
    mrn         TEXT,                          -- medical record number (NOT used as a match key)
    name        TEXT NOT NULL,                 -- legacy/composed; derived from first_name+last_name when structured input is used
    first_name  TEXT,                          -- structured (migration 011); NULL for legacy-only callers
    last_name   TEXT,                          -- structured (migration 011); NULL for legacy-only callers
    dob         TEXT,                          -- stored as ISO string, not DATE
    ssn         TEXT,                          -- plain text
    ssn_digits  TEXT GENERATED ALWAYS AS (regexp_replace(ssn, '\D', '', 'g')) STORED,
                                               -- migration 015: indexed digit-only match key for reconciliation
    gender      TEXT,
    address     TEXT,                          -- legacy/composed full address; derived from address+city+state+zip_code when structured input is used
    city        TEXT,                          -- structured (migration 011); NULL for legacy-only callers
    state       TEXT,                          -- structured (migration 011); NULL for legacy-only callers
    zip_code    TEXT,                          -- structured (migration 011), TEXT to preserve leading zeros / ZIP+4
    phone       TEXT,
    email       TEXT,
    notes       TEXT,                          -- free-text clinical notes, plain text
    created_via TEXT,                          -- self_service | front_desk
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS patients_ssn_digits_idx ON patients (ssn_digits);
-- NOTE: still no UNIQUE constraint on (name, dob, ssn) — self-service intake
-- can still fork one person into several rows. Week 2-3 catch-up
-- (adr/0004, RIV-160) added a deterministic (dob, ssn) match-key lookup at
-- intake and the patient_links table below to make a duplicate reviewable
-- instead of a silent, untracked fragment — see
-- services/intake-service/app.py::_find_match_candidates. It does not
-- retroactively merge or backfill any duplicate that already existed
-- before this migration (adr/0004 explicitly scopes that out).

CREATE TABLE IF NOT EXISTS patient_links (
    id                SERIAL PRIMARY KEY,
    patient_id        INTEGER NOT NULL REFERENCES patients(id),
    linked_patient_id INTEGER NOT NULL REFERENCES patients(id),
    confidence        TEXT NOT NULL CHECK (confidence IN ('exact', 'partial')),
    basis             TEXT,
    confirmed         BOOLEAN NOT NULL DEFAULT FALSE,
    confirmed_by      TEXT,
    confirmed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (patient_id <> linked_patient_id)
);
CREATE INDEX IF NOT EXISTS patient_links_patient_id_idx ON patient_links (patient_id);
CREATE INDEX IF NOT EXISTS patient_links_linked_patient_id_idx ON patient_links (linked_patient_id);

-- Week 4 catch-up (migration 014): the patient-ownership/care-team-membership
-- fact RIV-201 identified as missing — see docs/analysis/RIV-201-patient-
-- records-IDOR.md §6. `username` mirrors AuthorizationRequest.actor_id (the
-- gateway's X-Actor-Id, i.e. session.get("username")) exactly. Action/purpose
-- stay enforced in code (see libs/patient_view_agent), not as columns here.
CREATE TABLE IF NOT EXISTS patient_access_grants (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ,             -- NULL = active; set = explicitly revoked
    expires_at  TIMESTAMPTZ,             -- NULL = never expires
    UNIQUE (user_id, patient_id)
);
CREATE INDEX IF NOT EXISTS patient_access_grants_user_id_idx ON patient_access_grants (user_id);
CREATE INDEX IF NOT EXISTS patient_access_grants_patient_id_idx ON patient_access_grants (patient_id);

-- users.patient_id's foreign key, attached here because `patients` does not
-- exist yet where the column is declared above.
ALTER TABLE users ADD CONSTRAINT users_patient_id_fkey
    FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX IF NOT EXISTS users_patient_id_unique
    ON users (patient_id) WHERE patient_id IS NOT NULL;

-- Clinic-issued patient portal invitations (017). The code itself is never
-- stored — only its hash, as with passwords: an invitation code is a
-- credential for a chart. At most one live invitation per patient, so a second
-- code cannot quietly become a second way in.
CREATE TABLE IF NOT EXISTS patient_invitations (
    id                SERIAL PRIMARY KEY,
    patient_id        INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    code_hash         TEXT NOT NULL,
    issued_by         INTEGER NOT NULL REFERENCES users(id),
    issued_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    activated_at      TIMESTAMPTZ,
    revoked_at        TIMESTAMPTZ,
    activated_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS patient_invitations_code_hash_idx
    ON patient_invitations (code_hash);
-- At most one LIVE invitation per patient: a second live code is a second way
-- in, and revoking one would not close the other.
CREATE UNIQUE INDEX IF NOT EXISTS patient_invitations_one_live_per_patient
    ON patient_invitations (patient_id)
    WHERE activated_at IS NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS patient_invitations_patient_id_idx
    ON patient_invitations (patient_id);


CREATE TABLE IF NOT EXISTS insurance_coverages (
    id            SERIAL PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    payer_name    TEXT,
    member_id     TEXT,
    group_number  TEXT,
    plan_type     TEXT,                        -- PPO | HMO | Medicaid | Medicare | self_pay
    status        TEXT DEFAULT 'unknown'        -- active | inactive | unknown | pending | stale
                  CHECK (status IN ('active', 'inactive', 'unknown', 'pending', 'stale')),
    status_legacy TEXT,                         -- pre-migration-009 value, only set if it was remapped
    verified_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Migration 023 (W9.3) — the current/last eligibility-service job for
    -- THIS coverage. eligibility-service's own job record stores no
    -- patient_id/coverage_id by design; this is the association kept on
    -- the coverage side instead, and never sent to the browser.
    verification_job_id TEXT
);

-- ---------------------------------------------------------------------------
-- Scheduling
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS providers (
    id        SERIAL PRIMARY KEY,
    name      TEXT NOT NULL,
    specialty TEXT,
    location  TEXT
);

CREATE TABLE IF NOT EXISTS slots (
    id          SERIAL PRIMARY KEY,
    provider_id INTEGER REFERENCES providers(id),
    location    TEXT,
    start_at    TIMESTAMPTZ NOT NULL,
    end_at      TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'open'   -- open | booked (advisory only)
);

CREATE TABLE IF NOT EXISTS appointments (
    id                      SERIAL PRIMARY KEY,
    patient_id              INTEGER NOT NULL REFERENCES patients(id),
    slot_id                 INTEGER NOT NULL,   -- NOTE: no FK to slots(id) yet
    provider                TEXT,
    reason                  TEXT,
    location                TEXT,
    scheduled_for           TIMESTAMPTZ,
    status                  TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed | cancelled | cancelled_duplicate | completed
    created_at              TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    -- Stage 4 (Week 5, RIV-175, migration 013): idempotency_key lets a client
    -- retry of a slow POST return the original booking instead of racing a
    -- second insert; NULL for every pre-migration row. reconciled_duplicate_of
    -- is set only on a 'cancelled_duplicate' row, pointing at the appointment
    -- it lost the confirmed slot to during migration 013's one-time
    -- reconciliation.
    idempotency_key         TEXT,
    reconciled_duplicate_of INTEGER REFERENCES appointments(id)
);

-- migration 013: at most one CONFIRMED appointment per slot — the actual fix
-- for RIV-175's double-booking race. Partial (not a plain UNIQUE on slot_id)
-- so a slot can be legitimately rebooked once its one confirmed appointment
-- is cancelled.
CREATE UNIQUE INDEX IF NOT EXISTS appointments_confirmed_slot_unique
    ON appointments(slot_id) WHERE status = 'confirmed';

-- migration 013: a client-supplied idempotency key is unique per patient, not
-- globally — two different patients may coincidentally reuse the same key.
CREATE UNIQUE INDEX IF NOT EXISTS appointments_idempotency_key_unique
    ON appointments(patient_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Clinical records
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS encounters (
    id             SERIAL PRIMARY KEY,
    patient_id     INTEGER NOT NULL REFERENCES patients(id),
    encounter_type TEXT,                       -- office_visit, lab, imaging, telehealth
    provider       TEXT,
    reason         TEXT,
    location       TEXT,
    status         TEXT DEFAULT 'finished',
    summary        TEXT,
    allergies      TEXT,                       -- comma-separated, free text
    medications    TEXT,                       -- comma-separated, free text
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- records search hits body with no supporting index (full scan)
CREATE TABLE IF NOT EXISTS records (
    id              SERIAL PRIMARY KEY,
    encounter_id    INTEGER NOT NULL REFERENCES encounters(id),
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    kind            TEXT,                       -- lab_result | note | imaging | immunization
    title           TEXT,
    body            TEXT,
    status          TEXT,                       -- final | preliminary | normal | abnormal
    reference_range TEXT,                       -- for lab results
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS consents (
    id          SERIAL PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    kind        TEXT,                          -- npp_ack | treatment_consent | roi_consent
    signed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- "Audit" log. Ordinary mutable table; rows can be UPDATE/DELETEd and
-- soft-deleted. Currently we mostly dump request info here. This is logging,
-- not tamper-evident auditing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id          SERIAL PRIMARY KEY,
    actor       TEXT,
    message     TEXT,                          -- often the full request body
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ                     -- soft delete
);

-- ---------------------------------------------------------------------------
-- Role migration accounting (019, branch 9 part 2)
-- ---------------------------------------------------------------------------
-- One row per account the roster migration touched. The client signs a mapping
-- report and then a migration runs; this is the record of what actually
-- changed, and it is the unmapped-account list they asked to receive back.
--
-- Append-only BY CONVENTION, not by enforcement. Tamper-evident audit is
-- separate, unstarted work — do not describe this table as protected.
CREATE TABLE IF NOT EXISTS role_migration_log (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL,           -- not a FK: the record must outlive the account
    from_role     TEXT,
    to_role       TEXT,                    -- NULL when the outcome was not a migration
    outcome       TEXT NOT NULL,
    detail        TEXT,
    roster_name   TEXT,
    approved_by   TEXT NOT NULL,           -- who signed the report this run applied
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS role_migration_log_username_idx ON role_migration_log (username);
CREATE INDEX IF NOT EXISTS role_migration_log_outcome_idx ON role_migration_log (outcome);

-- ---------------------------------------------------------------------------
-- Agentic draft provenance (020) — the clinical artifact plus its metadata
-- ---------------------------------------------------------------------------
-- Kept in sync with db/migrations/020_agent_draft_provenance.sql — see that
-- file for the full reasoning (persistence boundary, transition graph,
-- provenance truthfulness, reapplication safety). Constraints here are
-- written inline, unlike the migration's guarded ALTER TABLE form, because
-- this file only ever CREATEs a fresh table on an empty volume.
--
-- generated_text IS PHI and IS persisted, deliberately (adr/0010): a model
-- response is not reproducible, so regenerating at display could show a patient
-- something no clinician approved. The prohibition on persisting model output is
-- scoped to logs, traces, telemetry and prompts — libs/agent_provenance raises
-- if draft text is put into a trace. Identity/evidence is immutable and status
-- only advances along a fixed transition graph, both enforced by trigger below.
CREATE TABLE IF NOT EXISTS agent_draft_provenance (
    id              SERIAL PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,

    -- Monotonic per patient. Version 2 never mutates version 1: a regenerated
    -- draft is a new row, so "which version did the clinician approve" stays
    -- answerable after a regeneration.
    version         INTEGER NOT NULL,

    -- draft -> validated -> approved -> superseded, or draft -> refused /
    -- validated -> rejected. refused/rejected/superseded are terminal. See
    -- the transition-guard trigger below, which enforces this graph directly.
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'validated', 'refused',
                                      'approved', 'rejected', 'superseded')),

    -- real   = a live provider call produced this
    -- fixture= a recorded response was replayed
    -- fallback = the deterministic path ran; no model was called
    -- The client requires these labels to be explicit. `fallback` text must
    -- never be presented as model output. See agent_draft_provenance_truthful
    -- below for what each label requires of model_id/prompt_version.
    provenance_label TEXT NOT NULL
                     CHECK (provenance_label IN ('real', 'fixture', 'fallback')),

    -- Ties every stage of one request together: retrieval, provider call,
    -- validation, review, display. Not a secret and not patient data.
    correlation_id  TEXT NOT NULL,

    -- Model identity is configuration, not output. Recorded so a demo can say
    -- which model produced a draft; NULL for fallback, required for real/fixture.
    model_id        TEXT,

    -- Machine-readable outcome of deterministic validation. A reason code, not
    -- a message: a validator message could quote the text it rejected. NULL
    -- while status='draft'; 'PASS' for every post-validation non-refused state;
    -- a specific non-PASS code while 'refused'.
    validation_code TEXT,

    -- THE CLINICAL ARTIFACT. Immutable once written — see the trigger below.
    -- This is what the clinician reviews and what the patient is shown; it is
    -- never regenerated at display time.
    generated_text  TEXT NOT NULL,

    -- Which prompt produced it. A version identifier, NOT the prompt itself:
    -- the prompt text stays out of the database exactly as it stays out of
    -- traces. NULL for fallback, required for real/fixture, same as model_id.
    prompt_version  TEXT,

    -- Reviewer / editor, by user id. An id is a reference; a username is an
    -- identifier, and identifiers do not belong in metadata columns.
    reviewed_by     INTEGER REFERENCES users(id),
    approved_at     TIMESTAMPTZ,
    rejected_at     TIMESTAMPTZ,

    -- Exact per-status decision-completeness — not merely "decided XOR
    -- undecided" (that would let an 'approved' row carry rejected_at). Mirrors
    -- migration 018's default-deny pattern, extended to this table's four
    -- decided/undecided states. 'superseded' retains the original approval's
    -- decider/timestamp: a supersede is a newer version taking over, not an
    -- un-approval.
    CONSTRAINT agent_draft_decision_complete CHECK (
        (status IN ('draft', 'validated', 'refused')
            AND reviewed_by IS NULL AND approved_at IS NULL AND rejected_at IS NULL)
        OR (status = 'approved'
            AND reviewed_by IS NOT NULL AND approved_at IS NOT NULL AND rejected_at IS NULL)
        OR (status = 'rejected'
            AND reviewed_by IS NOT NULL AND rejected_at IS NOT NULL AND approved_at IS NULL)
        OR (status = 'superseded'
            AND reviewed_by IS NOT NULL AND approved_at IS NOT NULL AND rejected_at IS NULL)
    ),

    -- Provenance truthfulness. NULL-safe: every nullable column compared here
    -- is guarded with an explicit IS [NOT] NULL, because a bare equality
    -- against a NULL evaluates to NULL, and Postgres does not treat a CHECK's
    -- NULL result as a violation — an unguarded `model_id = 'x'` would
    -- silently let a NULL model_id through.
    CONSTRAINT agent_draft_provenance_truthful CHECK (
        (provenance_label = 'fallback'
            AND model_id IS NULL AND prompt_version IS NULL)
        OR (provenance_label IN ('real', 'fixture')
            AND model_id IS NOT NULL AND btrim(model_id) <> ''
            AND prompt_version IS NOT NULL AND btrim(prompt_version) <> '')
    ),

    -- Closes the "empty string" loophole NOT NULL does not catch.
    CONSTRAINT agent_draft_correlation_id_nonblank CHECK (btrim(correlation_id) <> ''),
    CONSTRAINT agent_draft_generated_text_nonblank CHECK (btrim(generated_text) <> ''),
    CONSTRAINT agent_draft_version_positive CHECK (version > 0),

    -- Validation-code consistency — same NULL-safety discipline as
    -- agent_draft_provenance_truthful above.
    CONSTRAINT agent_draft_validation_code_consistent CHECK (
        (status = 'draft' AND validation_code IS NULL)
        OR (status IN ('validated', 'approved', 'rejected', 'superseded')
            AND validation_code IS NOT NULL AND validation_code = 'PASS')
        OR (status = 'refused'
            AND validation_code IS NOT NULL AND btrim(validation_code) <> ''
            AND validation_code <> 'PASS')
    ),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (patient_id, version)
);

CREATE INDEX IF NOT EXISTS agent_draft_provenance_patient_idx
    ON agent_draft_provenance (patient_id);
CREATE INDEX IF NOT EXISTS agent_draft_provenance_correlation_idx
    ON agent_draft_provenance (correlation_id);

-- At most one PATIENT-VISIBLE version at a time, enforced by a unique index
-- rather than an application-level check-then-insert — concurrency-safe by
-- construction. A regeneration must move the old approved row to 'superseded'
-- in the SAME transaction that approves the new version, or the new approval
-- is rejected by this index.
CREATE UNIQUE INDEX IF NOT EXISTS agent_draft_one_approved_per_patient
    ON agent_draft_provenance (patient_id)
    WHERE status = 'approved';

-- One row per citation a draft made. Source id AND version, because an approved
-- document can be superseded and an approval must remain interpretable against
-- the version that was actually cited.
CREATE TABLE IF NOT EXISTS agent_draft_citation (
    id              SERIAL PRIMARY KEY,
    draft_id        INTEGER NOT NULL REFERENCES agent_draft_provenance(id) ON DELETE CASCADE,
    source_id       TEXT NOT NULL,
    source_version  TEXT NOT NULL,
    citation_id     TEXT NOT NULL,
    category        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A citation naming an empty source/citation id is not evidence of
    -- anything and would defeat the "cited ids are a subset of retrieved ids"
    -- check in libs/patient_view_agent/composer.py by comparing against blanks.
    CONSTRAINT agent_draft_citation_fields_nonblank CHECK (
        btrim(source_id) <> '' AND btrim(source_version) <> '' AND btrim(citation_id) <> ''
    ),

    UNIQUE (draft_id, citation_id)
);

CREATE INDEX IF NOT EXISTS agent_draft_citation_draft_idx
    ON agent_draft_citation (draft_id);
CREATE INDEX IF NOT EXISTS agent_draft_citation_source_idx
    ON agent_draft_citation (source_id, source_version);

-- IDENTITY, EVIDENCE AND LIFECYCLE GUARD. A CHECK constraint only sees one
-- row's final state; these four guarantees each compare OLD to NEW, or must
-- run on DELETE (which no CHECK ever sees), so they must be a trigger:
-- (1) patient_id, version, generated_text, provenance_label, correlation_id,
-- model_id and prompt_version never change after insert; (2) validation_code
-- is set exactly once, when the row leaves 'draft'; (3) status only advances
-- along the transition graph documented on the table above — every other
-- move, including any move out of a terminal state, is rejected; (4) a row
-- that has left 'draft' can never be DELETEd, deterministically and
-- independent of whether it has any citations (a plain cascade-only guard, as
-- this once was, leaves a decided draft with zero citations unprotected).
CREATE OR REPLACE FUNCTION agent_draft_provenance_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION
                'agent_draft_provenance rows are never deleted once they leave '
                '''draft'' status (draft id=%, status=%). Deletion policy beyond '
                'this refusal is w8-planner-2 B3.', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;

    -- TG_OP = 'UPDATE' from here on.
    IF NEW.patient_id IS DISTINCT FROM OLD.patient_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.generated_text IS DISTINCT FROM OLD.generated_text
       OR NEW.provenance_label IS DISTINCT FROM OLD.provenance_label
       OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
       OR NEW.model_id IS DISTINCT FROM OLD.model_id
       OR NEW.prompt_version IS DISTINCT FROM OLD.prompt_version
    THEN
        RAISE EXCEPTION
            'agent_draft_provenance identity/evidence is immutable once written '
            '(draft id=%, version=%): patient_id, version, generated_text, '
            'provenance_label, correlation_id, model_id and prompt_version never '
            'change after insert. A corrected or regenerated draft is a NEW version.',
            OLD.id, OLD.version;
    END IF;

    IF NEW.validation_code IS DISTINCT FROM OLD.validation_code
       AND OLD.status <> 'draft'
    THEN
        RAISE EXCEPTION
            'agent_draft_provenance.validation_code may only be set once, when '
            'leaving draft status (draft id=%, current status=%)', OLD.id, OLD.status;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'draft'        AND NEW.status IN ('validated', 'refused'))
            OR (OLD.status = 'validated' AND NEW.status IN ('approved', 'rejected'))
            OR (OLD.status = 'approved'  AND NEW.status = 'superseded')
        ) THEN
            RAISE EXCEPTION
                'invalid agent_draft_provenance status transition % -> % (draft id=%). '
                'Allowed: draft->validated|refused, validated->approved|rejected, '
                'approved->superseded. refused/rejected/superseded are terminal.',
                OLD.status, NEW.status, OLD.id;
        END IF;
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_draft_immutable_text ON agent_draft_provenance;
DROP TRIGGER IF EXISTS agent_draft_provenance_guard_trigger ON agent_draft_provenance;
CREATE TRIGGER agent_draft_provenance_guard_trigger
    BEFORE UPDATE OR DELETE ON agent_draft_provenance
    FOR EACH ROW EXECUTE FUNCTION agent_draft_provenance_guard();

DROP FUNCTION IF EXISTS agent_draft_text_is_immutable();

-- CITATION IMMUTABILITY GUARD. UPDATE is forbidden outright. INSERT is
-- allowed only while the parent draft is still 'draft'. DELETE is allowed
-- while the parent is still 'draft', OR when the parent row is no longer
-- found at all — which, given the parent guard above only ever permits
-- deleting a 'draft' parent, can only mean this DELETE is running as part of
-- the FK's ON DELETE CASCADE from that already-permitted deletion (the parent
-- tuple is gone before the cascade fires this trigger, in the same
-- transaction). Treating "parent not found" as forbidden — as an earlier
-- version of this file did — made it impossible to ever delete a draft
-- parent that had any citations, since its own legitimate cascade would trip
-- this exact guard.
--
-- CONCURRENCY: both lookups take `FOR SHARE` on the parent row, not a plain
-- SELECT, so a citation write and a concurrent status-transition UPDATE on
-- the same draft are always serialized against each other rather than racing
-- past one another's uncommitted state.
CREATE OR REPLACE FUNCTION agent_draft_citation_guard()
RETURNS TRIGGER AS $$
DECLARE
    parent_status TEXT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'agent_draft_citation rows are immutable — no field may be changed '
            'once written (citation id=%). A corrected citation set belongs to a '
            'NEW draft version.', OLD.id;
    ELSIF TG_OP = 'INSERT' THEN
        SELECT status INTO parent_status
            FROM agent_draft_provenance WHERE id = NEW.draft_id
            FOR SHARE;
        IF parent_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION
                'a citation may only be added while its draft is still in '
                '''draft'' status (draft id=%, status=%)',
                NEW.draft_id, COALESCE(parent_status, '<no such draft>');
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status
            FROM agent_draft_provenance WHERE id = OLD.draft_id
            FOR SHARE;
        IF parent_status IS NOT NULL AND parent_status <> 'draft' THEN
            RAISE EXCEPTION
                'a citation may not be removed once its draft has left ''draft'' '
                'status (citation id=%, draft status=%)',
                OLD.id, parent_status;
        END IF;
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_draft_citation_guard_trigger ON agent_draft_citation;
CREATE TRIGGER agent_draft_citation_guard_trigger
    BEFORE INSERT OR UPDATE OR DELETE ON agent_draft_citation
    FOR EACH ROW EXECUTE FUNCTION agent_draft_citation_guard();

-- ---------------------------------------------------------------------------
-- Release of Information (ROI)
-- ---------------------------------------------------------------------------
-- A request to release records to a third party. There is no column for a
-- signed 45 CFR 164.508 authorization and no enforcement that one exists, and
-- no place to record 164.522 agreed restrictions.
CREATE TABLE IF NOT EXISTS roi_requests (
    id               SERIAL PRIMARY KEY,
    patient_id       INTEGER NOT NULL REFERENCES patients(id),
    requested_by     TEXT,
    recipient        TEXT,
    recipient_type   TEXT,                     -- self | provider | attorney | payer
    purpose          TEXT,
    date_range_start TEXT,
    date_range_end   TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending | fulfilled | denied
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    -- no authorization_id, no signed-authorization reference, no restriction tracking
);

-- Disclosures (what actually went out). Still missing the authorization linkage
-- and purpose, so an accounting-of-disclosures cannot be produced.
CREATE TABLE IF NOT EXISTS disclosures (
    id              SERIAL PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    roi_request_id  INTEGER REFERENCES roi_requests(id),
    disclosed_to    TEXT,
    disclosed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    -- no authorization_id, no purpose, no restriction tracking
);

-- ---------------------------------------------------------------------------
-- RAG corpus embeddings (see db/migrations/010_pgvector_embeddings.sql)
-- ---------------------------------------------------------------------------
-- Persists libs/rag_corpus's embed-once pipeline output behind a pgvector ANN
-- retrieval path (libs/rag_corpus/vector_store.py::PgVectorStore). patient_id
-- lives on the same row as its embedding so the ANN search and the
-- patient-scope predicate are one filtered query under the existing Postgres
-- ACLs (adr/0006 §2) — defense in depth for this retrieval path specifically,
-- not a fix for the unresolved RIV-201 gateway/records IDOR. No record text
-- or other PHI is stored here, only the vector and its identifiers.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_embeddings (
    id           SERIAL PRIMARY KEY,
    record_id    TEXT NOT NULL,             -- CorpusRecord.record_id, e.g. seed-enc-0001
    patient_id   INTEGER NOT NULL REFERENCES patients(id),
    provider     TEXT NOT NULL,             -- embedding provider tag (fake | ollama)
    model        TEXT NOT NULL DEFAULT '',  -- e.g. OLLAMA_EMBED_MODEL; '' for the fake provider
    dimension    INTEGER NOT NULL,
    content_hash TEXT NOT NULL,             -- sha256 of the embedded text; drives re-embed/re-write skip
    embedding    VECTOR(16) NOT NULL,       -- matches migration 010 and FakeEmbeddingProvider (16-dim)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (record_id, provider, model)
);

CREATE INDEX IF NOT EXISTS rag_embeddings_patient_id_idx ON rag_embeddings (patient_id);

CREATE INDEX IF NOT EXISTS rag_embeddings_hnsw_idx
    ON rag_embeddings USING hnsw (embedding vector_cosine_ops);

-- --------------------------------------------------------------------------
-- patient_summary_reviews — the clinician gate (migration 018)
--
-- Kept in sync with db/migrations/018_patient_summary_reviews.sql. This file
-- is the flattened schema a fresh volume is built from, and records-service
-- queries this table on every patient summary read — so omitting it here
-- breaks a fresh `make up` before anyone runs a migration by hand.
--
-- DEFAULT DENY: a patient sees refused content only via an explicit
-- `approved` row. See the migration for the full reasoning.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS patient_summary_reviews (
    id          SERIAL PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    record_id   INTEGER NOT NULL REFERENCES records(id)  ON DELETE CASCADE,
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'approved', 'rejected')),
    reason      TEXT,
    decided_by  INTEGER REFERENCES users(id),
    decided_at  TIMESTAMPTZ,
    decision_note TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A decided row must name its decider; a pending row must not have one.
    CONSTRAINT patient_summary_reviews_decision_complete CHECK (
        (state = 'pending'  AND decided_by IS NULL AND decided_at IS NULL)
        OR
        (state <> 'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS patient_summary_reviews_one_pending_per_record
    ON patient_summary_reviews (record_id) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS patient_summary_reviews_record_state_idx
    ON patient_summary_reviews (record_id, state);
CREATE INDEX IF NOT EXISTS patient_summary_reviews_pending_idx
    ON patient_summary_reviews (state, created_at DESC);
CREATE INDEX IF NOT EXISTS patient_summary_reviews_patient_idx
    ON patient_summary_reviews (patient_id);

-- --------------------------------------------------------------------------
-- message_threads / thread_messages / thread_read_state — secure
-- patient-clinician messaging (migration 022)
--
-- Kept in sync with db/migrations/022_message_threads.sql. Authorization
-- reuses patient_access_grants exactly as chart access does — see that
-- migration's own comments and services/records-service/app.py's messaging
-- routes.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_threads (
    id          SERIAL PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    subject     TEXT NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 200),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS message_threads_patient_idx
    ON message_threads (patient_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS thread_messages (
    id              SERIAL PRIMARY KEY,
    thread_id       INTEGER NOT NULL REFERENCES message_threads(id) ON DELETE CASCADE,
    sender_user_id  INTEGER NOT NULL REFERENCES users(id),
    body            TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
    idempotency_key TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS thread_messages_sender_thread_idem_key
    ON thread_messages (sender_user_id, thread_id, idempotency_key);
CREATE INDEX IF NOT EXISTS thread_messages_thread_idx
    ON thread_messages (thread_id, created_at);

CREATE TABLE IF NOT EXISTS thread_read_state (
    thread_id             INTEGER NOT NULL REFERENCES message_threads(id) ON DELETE CASCADE,
    user_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_read_message_id  INTEGER REFERENCES thread_messages(id),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, user_id)
);

-- ---------------------------------------------------------------------------
-- policy_documents / policy_chunks — synthetic policy RAG corpus foundation
-- (w-9-2-planner P2). Kept in sync with db/migrations/024_policy_corpus.sql.
--
-- Deliberately separate from rag_embeddings (migration 010): that table is
-- patient-scoped (mandatory patient_id FK) and fixed at VECTOR(16) for the
-- fake embedding provider; policy retrieval is audience/workflow-scoped, not
-- patient-scoped, and has no embedding column yet — see the migration file
-- for why that is deferred rather than added speculatively.
--
-- No patient facts, message bodies, prompts, or provider responses belong in
-- either table — only the manifest's own declared metadata and synthetic
-- document text (docs/RagDocs/manifest.json's `patient_data_in_corpus: false`).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS policy_documents (
    id                SERIAL PRIMARY KEY,
    corpus_id         TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    source_version    TEXT NOT NULL,
    title             TEXT NOT NULL,
    owner             TEXT NOT NULL,
    effective_date    DATE,
    approval_status   TEXT NOT NULL,
    synthetic         BOOLEAN NOT NULL,
    retrieval_enabled BOOLEAN NOT NULL,
    content_path      TEXT NOT NULL,
    content_sha256    TEXT NOT NULL,
    audiences         TEXT[] NOT NULL DEFAULT '{}',
    workflows         TEXT[] NOT NULL DEFAULT '{}',
    topics            TEXT[] NOT NULL DEFAULT '{}',
    allowed_uses      TEXT[] NOT NULL DEFAULT '{}',
    prohibited_uses   TEXT[] NOT NULL DEFAULT '{}',
    relationships     JSONB NOT NULL DEFAULT '[]',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_version)
);

CREATE INDEX IF NOT EXISTS policy_documents_audiences_gin_idx ON policy_documents USING gin (audiences);
CREATE INDEX IF NOT EXISTS policy_documents_workflows_gin_idx ON policy_documents USING gin (workflows);

CREATE TABLE IF NOT EXISTS policy_chunks (
    id             SERIAL PRIMARY KEY,
    chunk_id       TEXT NOT NULL UNIQUE,
    document_id    INTEGER NOT NULL REFERENCES policy_documents(id) ON DELETE CASCADE,
    section_id     TEXT NOT NULL,
    heading_path   TEXT[] NOT NULL DEFAULT '{}',
    text           TEXT NOT NULL,
    chunk_hash     TEXT NOT NULL,
    char_count     INTEGER NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS policy_chunks_document_id_idx ON policy_chunks (document_id);

-- ---------------------------------------------------------------------------
-- policy_chunk_embeddings — kept in sync with
-- db/migrations/025_policy_chunk_embeddings.sql. Separate from policy_chunks
-- itself (mirrors rag_embeddings' own separation of record identity from its
-- embedding); fixed at VECTOR(1024) for amazon.titan-embed-text-v2:0. No
-- HNSW/ANN index — an exact scan is the deliberate choice at this corpus's
-- scale (vector-rag.md).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS policy_chunk_embeddings (
    id           SERIAL PRIMARY KEY,
    chunk_id     TEXT NOT NULL REFERENCES policy_chunks(chunk_id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,
    model        TEXT NOT NULL,
    dimension    INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    embedding    VECTOR(1024) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, provider, model)
);

CREATE INDEX IF NOT EXISTS policy_chunk_embeddings_chunk_id_idx ON policy_chunk_embeddings (chunk_id);

-- ---------------------------------------------------------------------------
-- Runtime role privileges (028, w8-planner-2 P3 — closes AUD-B01, a code
-- review finding on PR #84). db/docker-init/00-create-app-role.sh creates
-- the runtime role before this file runs, LOGIN but owning nothing and
-- never superuser — this section is what actually grants it the ability to
-- do its job now that every table above exists. Kept in sync with
-- db/migrations/028_admin_runtime_role_separation.sql, which applies the
-- same grants (plus the one-time ownership transfer) to a volume that
-- predates this split; duplicated between the two for the same reason
-- schema.sql duplicates every other migration's cumulative effect.
--
-- No hard-coded role name (round-3 review): :"app_user" is a safely quoted
-- psql identifier substitution, read via \getenv from DB_APP_USER — this
-- file's own process environment (docker-compose.yml bakes it into the
-- postgres container; db/docker-init/01-run-schema.sh is the wrapper that
-- actually invokes psql for this file, since docker-entrypoint-initdb.d's
-- own built-in .sql handling has no \getenv support at all). `make seed`
-- and any other manual `psql ... < db/schema.sql` run inside that same
-- container inherit the identical environment, so no separate threading is
-- needed there.
--
-- Broad grant first, then a narrower carve-out for audit_logs: no UPDATE or
-- DELETE there, and — because ALTER TABLE requires ownership, not a
-- grantable privilege — no ability to disable its triggers either, which is
-- the actual fix (026's trigger alone could not stop the table's OWNER from
-- disabling it).
-- ---------------------------------------------------------------------------
\getenv app_user DB_APP_USER

GRANT USAGE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
REVOKE UPDATE, DELETE ON audit_logs FROM :"app_user";
