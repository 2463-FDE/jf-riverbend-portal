-- 033_mfa.sql — w8-planner-2 MFA rollout: TOTP second factor, backup codes,
-- and the columns a supervisor-authorized reset needs to clear atomically.
--
-- Supersedes the parked prototype (migration 016 on feat/mfa-totp-parked,
-- reverted 3c22a16 pending a complete rollout, not merged into this
-- history). This is that complete rollout's schema. Two differences from
-- the parked design, both deliberate:
--
--   1. mfa_secret is AEAD-encrypted (mfa_secret_ciphertext +
--      mfa_secret_key_version), not plaintext. adr/0012 established the
--      pattern (application-layer encryption, versioned key material) for
--      patients.ssn/dob/notes and agent_draft_provenance.generated_text —
--      a TOTP secret is at least as sensitive as those (it IS the second
--      factor; plaintext-at-rest defeats the point of having one) and gets
--      the same treatment. The KEY MATERIAL is deliberately its own
--      (MFA_ACTIVE_KEY_VERSION / MFA_ENCRYPTION_KEY_V<n>,
--      services/gateway/mfa_crypto.py), not PHI_ACTIVE_KEY_VERSION reused —
--      a TOTP secret and PHI are different data classes, and coupling their
--      rotation would be a surprise in both directions.
--   2. mfa_shared_account / mfa_pilot exist. The 2026-08-12 park decision
--      named "shared-front-desk-login remediation" and "pilot clinic" as
--      required, not optional, parts of a real rollout — this repo has no
--      staff-directory data to say which of the seeded/real accounts are
--      individually-owned versus a shared login multiple people sign into,
--      so that fact is modeled as an explicit column instead of guessed.
--      Defaults are fail-closed in the direction that matters here:
--      mfa_shared_account defaults TRUE (every existing account is treated
--      as NOT safe to enroll until someone explicitly says otherwise — a
--      shared login holding one person's TOTP secret locks out everyone
--      else who uses it, which is worse than not having MFA), and
--      mfa_pilot defaults FALSE (nobody is in the pilot scope until
--      explicitly opted in). See services/gateway/mfa_config.py and
--      docs/runbook.md for the operational dependency this leaves open:
--      classifying the real roster is client/ops work this migration
--      cannot do.
--
-- Guarded/idempotent, safe to re-apply (db/migrations/apply.sh). New table
-- grants are covered by migration 028's ALTER DEFAULT PRIVILEGES (the admin
-- role that runs this file grants the runtime role CRUD on every table it
-- creates), so no explicit GRANT is needed here.

ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret_ciphertext TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret_key_version TEXT;
-- Set only by a CONFIRMED enrollment (a verified code proved possession) —
-- see services/gateway/app.py::confirm_mfa_enrollment. A non-null
-- mfa_secret_ciphertext with a null mfa_enrolled_at means "a secret was
-- minted but never proven" — treat that as unenrolled everywhere, the same
-- as the parked design's mfa_secret/mfa_enrolled_at pairing.
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enrolled_at TIMESTAMPTZ;
-- The TOTP time-step (30s counter) last accepted for this user, so the
-- exact same code cannot be replayed a second time inside its own valid
-- window — see mfa_totp.verify_code's step-return contract. NULL until the
-- first successful verification.
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_last_totp_step BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_shared_account BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_pilot BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN users.mfa_secret_ciphertext IS
    'AEAD envelope (libs/phi_crypto envelope format) of the TOTP secret, encrypted under an MFA-specific key — see services/gateway/mfa_crypto.py. NULL means never enrolled.';
COMMENT ON COLUMN users.mfa_secret_key_version IS
    'Which MFA_ENCRYPTION_KEY_V<n> encrypted mfa_secret_ciphertext. Never falls back to another key on read (fail closed) — see mfa_crypto.decrypt_totp_secret.';
COMMENT ON COLUMN users.mfa_enrolled_at IS
    'Set only once a submitted code against mfa_secret_ciphertext has verified. NULL alongside a non-null mfa_secret_ciphertext = enrollment pending, not active.';
COMMENT ON COLUMN users.mfa_shared_account IS
    'TRUE (default) = not known to be an individually-owned login; never prompted or enforced for MFA regardless of rollout mode (services/gateway/mfa_config.py). Must be explicitly set FALSE per account before that account can be brought into scope — an operational dependency this migration does not resolve; see docs/runbook.md.';
COMMENT ON COLUMN users.mfa_pilot IS
    'Explicit pilot-scope opt-in. FALSE (default) = out of scope while config/mfa.yaml scope=pilot, regardless of mode. Never inferred from role/username.';

-- One-time recovery codes, generated as a batch of ten immediately after a
-- confirmed enrollment (services/gateway/mfa_backup_codes.py). Only a
-- salted hash is ever stored — the plaintext code is returned exactly once,
-- in the enrollment-confirmation (or regeneration) response body, and never
-- persisted or logged anywhere.
CREATE TABLE IF NOT EXISTS mfa_backup_codes (
    id             SERIAL PRIMARY KEY,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash      TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = unused. Set exactly once, by an atomic
    -- "UPDATE ... WHERE used_at IS NULL AND invalidated_at IS NULL"
    -- (services/gateway/app.py) so two concurrent redemption attempts of
    -- the same code cannot both succeed.
    used_at        TIMESTAMPTZ,
    -- Set by regeneration (services/gateway/app.py::regenerate_backup_codes)
    -- or by a supervisor reset — distinct from used_at so "why is this code
    -- no longer valid" is answerable from the row itself: consumed by the
    -- account holder, or invalidated out from under them by a new batch/a
    -- reset. A code is currently valid iff BOTH are NULL.
    invalidated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS mfa_backup_codes_user_id_idx ON mfa_backup_codes (user_id);
-- The scan every login-time backup-code check and every regeneration/reset
-- runs: "this user's currently-active codes." Partial so it stays small
-- regardless of how many historical (used/invalidated) rows accumulate.
CREATE INDEX IF NOT EXISTS mfa_backup_codes_active_idx ON mfa_backup_codes (user_id)
    WHERE used_at IS NULL AND invalidated_at IS NULL;
