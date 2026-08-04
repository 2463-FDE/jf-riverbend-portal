-- 001_init — baseline tables
-- (kept in sync with schema.sql by hand)
--
-- IF NOT EXISTS (Week 1 catch-up, deploy-safety fix): existing per-clinic
-- deployments have no recorded migration history to check against (schema.sql
-- is only ever applied once, on a fresh Postgres volume). Every statement in
-- db/migrations/ is guarded so db/migrations/apply.sh can safely re-run the
-- full set against a database at any prior point without erroring — see
-- docs/runbook.md "Deploying a new release."

CREATE TABLE IF NOT EXISTS patients (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    dob         TEXT,
    ssn         TEXT,
    address     TEXT,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS encounters (
    id             SERIAL PRIMARY KEY,
    patient_id     INTEGER NOT NULL REFERENCES patients(id),
    encounter_type TEXT,
    provider       TEXT,
    summary        TEXT,
    allergies      TEXT,
    medications    TEXT,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS records (
    id           SERIAL PRIMARY KEY,
    encounter_id INTEGER NOT NULL REFERENCES encounters(id),
    patient_id   INTEGER NOT NULL REFERENCES patients(id),
    kind         TEXT,
    body         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS appointments (
    id          SERIAL PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    slot_id     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'confirmed',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS consents (
    id          SERIAL PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    kind        TEXT,
    signed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          SERIAL PRIMARY KEY,
    actor       TEXT,
    message     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);
