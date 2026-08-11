-- 016_user_mfa — TOTP second factor (production-readiness Stage 1 item 3).
--
-- mfa_secret is set the first time a user with mfa_required=true logs in
-- (services/gateway/app.py::login) — it is NOT confirmed/active until that
-- same user successfully verifies one code (services/gateway/app.py::
-- login_mfa), which stamps mfa_enrolled_at. A non-null mfa_secret with a
-- null mfa_enrolled_at means "enrollment in progress, not yet proven the
-- user actually holds the secret" — treat it the same as unenrolled for any
-- decision that depends on MFA actually having been completed.
--
-- Stored as plaintext TEXT, consistent with this repo's existing plaintext-
-- PHI posture (adr/0002) — not a new gap, the same one already tracked.

ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_secret TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_enrolled_at TIMESTAMPTZ;
