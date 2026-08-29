-- 034_users_security_version — W10 Final Stage 1.
--
-- A live gateway session was not invalidated when the account behind it was
-- disabled or its role changed mid-session (require_session only checked
-- Redis's own TTLs, never re-read the account). This is the server-owned
-- counter that closes that gap: bumped whenever is_active/role changes,
-- copied into every new Redis session at login, and compared against the
-- CURRENT value on every authenticated request (services/gateway/app.py::
-- require_session). A mismatch — or is_active having gone false — kills the
-- session outright rather than letting it continue on stale state.

ALTER TABLE users ADD COLUMN IF NOT EXISTS security_version INTEGER NOT NULL DEFAULT 0;
