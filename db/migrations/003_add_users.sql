-- 003_add_users — portal + staff authentication
-- 2026-02-03 · Helix Digital Partners
-- Replaces the hard-coded demo login with a users table. One role for everyone
-- for now (see config/roles.yaml); least-privilege deferred to "phase 2".

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT,
    role          TEXT NOT NULL DEFAULT 'staff',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
