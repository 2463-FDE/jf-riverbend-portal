-- 014_patient_access_grants — the patient-ownership/care-team-membership
-- fact RIV-201 identified as missing (docs/analysis/RIV-201-patient-records-
-- IDOR.md §6, item 1: "does not exist today — users has no relationship to
-- patients"). Week 4 catch-up: this is the real, database-backed grant table
-- behind the new patient-specific AuthorizationPort implementation, replacing
-- the deny-by-default-but-not-patient-specific StaffAccessGate for the
-- production routes (services/records-service/app.py) and the gateway's
-- unauthorized proxy_patient/proxy_records IDOR.
--
-- Deliberately minimal (explicit-grant model, smallest safe option):
--   * `user_id` is the STABLE authenticated principal (users.id), the actor
--     identity the gateway forwards as X-Actor-Id after PR #23 review round 2
--     (2026-08-07). It is deliberately NOT `username`: a username can be
--     reassigned and is not a durable identity, and the Codex review flagged
--     keying authorization on it as unsafe. The gateway session carries
--     users.id; username is retained only as display/audit metadata
--     (X-Actor-Name). ON DELETE CASCADE drops a user's grants when the user
--     row is deleted.
--   * No action/purpose columns — those stay enforced in code exactly as
--     StaffAccessGate/FakePolicyAuthorization already do (an allow-list
--     constructor argument), so a grant is purely "may this actor ever act
--     on this patient," not a finer-grained ACL. Extending to per-grant
--     action/purpose is a real future need but was not asked for here.
--   * `revoked_at`/`expires_at` are both nullable so "denied" has two
--     independent, testable reasons distinct from "never granted at all":
--     an explicitly revoked grant, and a grant that lapsed on its own.
--     NULL revoked_at + (NULL or future expires_at) is the only ALLOW state.
--
-- This does NOT retroactively grant anyone access to anything — the table
-- starts empty in a real deployment; only the deterministic seed generator
-- (db/seed/generate_seed.py) populates rows, and only for its own synthetic
-- demo users/patients.
--
-- Codex review (2026-08-07, PR #23): an empty grant table means every
-- existing staff account is denied every existing patient the moment this
-- code deploys, with no in-app way to add a grant. This is a required,
-- explicit rollout step, not an automatic one — see docs/runbook.md
-- "Before enforcing migration 014 on any environment with real existing
-- patients" for why this can't be safely auto-backfilled and what an
-- operator needs to do before this code reaches a real environment.

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
