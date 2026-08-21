-- 019_role_migration_and_unmapped_denial — branch 9 part 2.
--
-- Part 1 (PR #49) made the dry-run report correct against the client's roster.
-- This is what applies it, and what an account the roster does not cover
-- experiences afterwards.
--
-- TWO THINGS ONLY, both additive and guarded so this file is safe to re-apply
-- against a database at any prior migration point (see apply.sh).
--
-- 1. users.disabled_reason — WHY an account is inactive, not just that it is.
--
--    `is_active` already existed and login already refused inactive accounts.
--    What it could not do is tell an operator, or the person locked out, the
--    difference between "your account was closed", "your shared login was
--    split into named accounts" and "the roster does not list you". The client
--    asked for one specific message for the last case, so the reason has to be
--    stored rather than inferred.
--
--    Deliberately TEXT with no enum/constraint: a new reason must not require
--    a migration in the middle of a cutover. The values this release writes
--    are 'role_migration_unmapped', 'role_migration_shared_login' and
--    'role_migration_no_owner'.
--
-- 2. role_migration_log — one row per account the migration touched.
--
--    The client signs a report and then a migration runs. Without a record,
--    "which accounts changed, from what, to what, and on whose sign-off" is
--    answerable only by reading application logs that rotate. This table is
--    the accounting artifact that goes back to the client, and it is the list
--    of unmapped accounts they asked to receive.
--
--    It is an append-only record BY CONVENTION here, not by enforcement —
--    tamper-evident audit is a separate, unstarted piece of work (C4). Do not
--    describe this table as protected.

ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled_reason TEXT;

CREATE TABLE IF NOT EXISTS role_migration_log (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL,           -- not a FK: the account may later be deleted,
                                           -- and the record of what happened must survive it
    from_role     TEXT,
    to_role       TEXT,                    -- NULL when the outcome was not a migration
    outcome       TEXT NOT NULL,            -- mirrors roster_dry_run.py's outcome constants
    detail        TEXT,
    roster_name   TEXT,                    -- the roster person matched, when there was one
    approved_by   TEXT NOT NULL,            -- who signed the report this run applied
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS role_migration_log_username_idx
    ON role_migration_log (username);
CREATE INDEX IF NOT EXISTS role_migration_log_outcome_idx
    ON role_migration_log (outcome);
