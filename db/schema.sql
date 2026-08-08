-- Riverbend Patient Portal — consolidated database schema (current state).
-- Postgres 15. All PHI is protected at the disk level (RDS volume encryption).
--
-- This file is the flattened "current" schema loaded by docker-entrypoint on a
-- fresh volume. The incremental history lives in db/migrations/*.sql and is kept
-- in sync with this file by hand (see ADR 0001 — no shared library / tooling yet).

-- ---------------------------------------------------------------------------
-- Authentication
-- ---------------------------------------------------------------------------
-- Portal + staff logins. Passwords are PBKDF2 (django-style string). Note:
-- there is exactly one role for everyone (see config/roles.yaml) and sessions
-- issued at login never expire (see services/gateway/auth.yaml).
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT,
    role          TEXT NOT NULL DEFAULT 'staff',   -- single role for everyone
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
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
    embedding    VECTOR(768) NOT NULL,      -- nomic-embed-text (Ollama) dimension; see migration 011
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (record_id, provider, model)
);

CREATE INDEX IF NOT EXISTS rag_embeddings_patient_id_idx ON rag_embeddings (patient_id);

CREATE INDEX IF NOT EXISTS rag_embeddings_hnsw_idx
    ON rag_embeddings USING hnsw (embedding vector_cosine_ops);
