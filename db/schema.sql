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
    name        TEXT NOT NULL,
    dob         TEXT,                          -- stored as ISO string, not DATE
    ssn         TEXT,                          -- plain text
    gender      TEXT,
    address     TEXT,
    phone       TEXT,
    email       TEXT,
    notes       TEXT,                          -- free-text clinical notes, plain text
    created_via TEXT,                          -- self_service | front_desk
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- NOTE: no unique match key on (name, dob, ssn) — self-service intake forks
-- one person into several rows. See intake.yaml match_key: none.

CREATE TABLE IF NOT EXISTS insurance_coverages (
    id            SERIAL PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    payer_name    TEXT,
    member_id     TEXT,
    group_number  TEXT,
    plan_type     TEXT,                        -- PPO | HMO | Medicaid | Medicare | self_pay
    status        TEXT DEFAULT 'unknown'        -- active | inactive | unknown | pending | stale
                  CHECK (status IN ('active', 'inactive', 'unknown', 'pending', 'stale')),
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
    id            SERIAL PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    slot_id       INTEGER NOT NULL,            -- NOTE: no UNIQUE constraint, no FK
    provider      TEXT,
    reason        TEXT,
    location      TEXT,
    scheduled_for TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'confirmed',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

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
