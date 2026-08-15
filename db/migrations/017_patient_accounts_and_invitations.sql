-- 017_patient_accounts_and_invitations — patient sign-in (S1).
--
-- The client chose clinic-issued invitation at registration: front desk
-- creates the link during registration and the patient activates it from a
-- code they are given. Chosen over MRN + date-of-birth self-registration
-- precisely because those are knowable by others, and would end up acting as
-- a credential for chart access.
--
-- AUTHORIZATION IS NOT EXTENDED. This is the point the client made directly:
-- a patient reading their own record is the same enforcement applied to a
-- different principal, not a second mechanism. An activated patient account is
-- a `users` row with `patient_id` set, plus ONE `patient_access_grants` row
-- for their own chart — so SqlPatientAccessGate authorizes them unchanged, and
-- every existing scoping query (list, search, reconciliation) already filters
-- them to their own record because that is the only grant they hold.
--
-- What keeps a patient off staff routes is the ROLE, not a separate code path:
-- the `patient` role in config/roles.yaml holds no staff permission at all.

-- users.patient_id — NULL for staff accounts, set for a patient's own account.
ALTER TABLE users ADD COLUMN IF NOT EXISTS patient_id INTEGER
    REFERENCES patients(id) ON DELETE RESTRICT;

-- One account per patient. Without this, a second invitation could quietly
-- mint a second credential for the same chart, and revoking the first would
-- not close access.
CREATE UNIQUE INDEX IF NOT EXISTS users_patient_id_unique
    ON users (patient_id) WHERE patient_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS patient_invitations (
    id            SERIAL PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,

    -- The code is never stored. Only a hash of it, exactly as passwords are
    -- handled — an invitation code is a credential for a chart, and a readable
    -- one in the database (or in a backup, or in a support screenshot) would
    -- be a chart-access credential lying in plain sight.
    code_hash     TEXT NOT NULL,

    issued_by     INTEGER NOT NULL REFERENCES users(id),   -- the staff member, for accounting
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    activated_at  TIMESTAMPTZ,                              -- NULL until redeemed; set once
    revoked_at    TIMESTAMPTZ,                              -- NULL = live

    -- Which account the redemption produced. NULL until activated; lets an
    -- auditor answer "who was given access to this chart, by whom, and when"
    -- without joining through timestamps.
    activated_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);

-- Redemption looks a code up by hash. Not unique: an expired or revoked
-- invitation stays on the table as a record, and a patient may legitimately be
-- re-invited, so the same patient can hold several rows over time.
CREATE INDEX IF NOT EXISTS patient_invitations_code_hash_idx
    ON patient_invitations (code_hash);

-- At most one LIVE invitation per patient. A second live code for the same
-- chart is a second way in, and revoking one would not close the other.
-- Expired, revoked and already-activated rows are excluded, so re-inviting
-- after any of those is fine.
CREATE UNIQUE INDEX IF NOT EXISTS patient_invitations_one_live_per_patient
    ON patient_invitations (patient_id)
    WHERE activated_at IS NULL AND revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS patient_invitations_patient_id_idx
    ON patient_invitations (patient_id);
