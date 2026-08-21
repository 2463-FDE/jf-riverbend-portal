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
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
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
-- Agentic draft provenance (020) — metadata only, NO content by design
-- ---------------------------------------------------------------------------
--
-- WHAT THIS TABLE HOLDS, AND WHAT IT DELIBERATELY DOES NOT
--
-- The client's constraint is explicit: persist only provenance metadata —
-- source id/version, citation ids, categories, status, timestamps, correlation
-- id. Never persist prompts, model output, retrieved text, patient data,
-- identifiers, credentials, or raw provider errors.
--
-- So there is NO content column here, and that is not an omission. Every column
-- below is metadata about a generation, not the generation.
--
-- ⚠️ OPEN DESIGN QUESTION, deliberately not answered by this migration.
-- `patient_summary_reviews` (018) also stores no text: approval points at a
-- record_id and the DETERMINISTIC renderer regenerates the summary at display
-- time. That works precisely because it is deterministic. A model-generated
-- draft is not reproducible, so "display exactly the approved version" cannot
-- be satisfied by regeneration. Whether the draft text is persisted, and where,
-- is a decision for the user — see w8-planner. This migration is additive and
-- does not foreclose either answer.
--
-- Additive and guarded, so it is safe to re-apply at any prior migration point
-- (see apply.sh).

CREATE TABLE IF NOT EXISTS agent_draft_provenance (
    id              SERIAL PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,

    -- Monotonic per patient. Version 2 never mutates version 1: a regenerated
    -- draft is a new row, so "which version did the clinician approve" stays
    -- answerable after a regeneration.
    version         INTEGER NOT NULL,

    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'validated', 'refused',
                                      'approved', 'rejected', 'superseded')),

    -- real   = a live provider call produced this
    -- fixture= a recorded response was replayed
    -- fallback = the deterministic path ran; no model was called
    -- The client requires these labels to be explicit. `fallback` text must
    -- never be presented as model output.
    provenance_label TEXT NOT NULL
                     CHECK (provenance_label IN ('real', 'fixture', 'fallback')),

    -- Ties every stage of one request together: retrieval, provider call,
    -- validation, review, display. Not a secret and not patient data.
    correlation_id  TEXT NOT NULL,

    -- Model identity is configuration, not output. Recorded so a demo can say
    -- which model produced a draft; NULL for fallback.
    model_id        TEXT,

    -- Machine-readable outcome of deterministic validation. A reason code, not
    -- a message: a validator message could quote the text it rejected.
    validation_code TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (patient_id, version)
);

CREATE INDEX IF NOT EXISTS agent_draft_provenance_patient_idx
    ON agent_draft_provenance (patient_id);
CREATE INDEX IF NOT EXISTS agent_draft_provenance_correlation_idx
    ON agent_draft_provenance (correlation_id);

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

    UNIQUE (draft_id, citation_id)
);

CREATE INDEX IF NOT EXISTS agent_draft_citation_draft_idx
    ON agent_draft_citation (draft_id);
CREATE INDEX IF NOT EXISTS agent_draft_citation_source_idx
    ON agent_draft_citation (source_id, source_version);

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
