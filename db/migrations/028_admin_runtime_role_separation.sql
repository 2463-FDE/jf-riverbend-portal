-- 028_admin_runtime_role_separation.sql — P3 audit integrity (w8-planner-2):
-- closes AUD-B01, a code review finding on PR #84. riverbend_app currently
-- owns every table it created (it is this cluster's original bootstrap
-- role), and a table's OWNER can always `ALTER TABLE ... DISABLE TRIGGER`
-- on it regardless of any REVOKE — Postgres checks ownership, not grantable
-- privileges, for ALTER TABLE. That made 026's append-only guard bypassable
-- by the very role that has real database access in this system; the
-- integration tests added alongside 026/027 demonstrate the exact path
-- (temporarily disabling the trigger to simulate corruption).
--
-- This is admin-versus-runtime privilege separation ONLY — not the deferred
-- per-service-credential project (adr/0001, still open: every service still
-- shares this one riverbend_app runtime credential). Only WHO can own or
-- ALTER schema objects changes; no service's connection code changes.
--
-- WHAT THIS DOES. Creates :'admin_user' (distinct from riverbend_app) if it
-- does not already exist, transfers ownership of every table, sequence, and
-- function riverbend_app currently owns in the public schema (plus the
-- database itself) to it, strips riverbend_app's superuser/createdb/
-- createrole bits, then grants riverbend_app exactly the runtime privileges
-- it needs — full CRUD on ordinary tables, but only INSERT + SELECT on
-- audit_logs. From this point riverbend_app cannot ALTER TABLE audit_logs
-- at all (that requires ownership), which is what actually closes AUD-B01.
--
-- WHY NOT REASSIGN OWNED BY (tried first, does not work here). On THIS
-- cluster's real starting state, riverbend_app is not just the application's
-- role — the official Postgres image makes whatever role POSTGRES_USER
-- names the actual initdb bootstrap role, which Postgres then also records
-- as the owner of pg_catalog, information_schema, pg_toast, and the
-- built-in internal/c/sql/plpgsql languages. `REASSIGN OWNED BY
-- riverbend_app TO x` tries to reassign ALL of that too and fails outright
-- with "cannot reassign ownership of objects owned by role riverbend_app
-- because they are required by the database system" — confirmed by direct
-- reproduction against a volume seeded from this repo's real schema.sql/
-- seed.sql. This migration instead targets exactly the objects the
-- application itself created: tables, sequences, and functions in the
-- public schema, plus the database. It deliberately does NOT reassign the
-- plpgsql/vector EXTENSIONS riverbend_app also owns — Postgres has no
-- `ALTER EXTENSION ... OWNER TO` (confirmed: not valid syntax), and
-- extension ownership does not grant any ownership-level control over
-- plain tables like audit_logs, so it is out of scope for what AUD-B01
-- actually requires.
--
-- TWO STARTING STATES, ONE IDEMPOTENT FILE.
--   Fresh volume: docker-compose.yml's postgres service boots with
--   POSTGRES_USER=:'admin_user' (db/docker-init/00-create-app-role.sh
--   creates riverbend_app separately, owning nothing) — schema.sql already
--   ran the equivalent GRANT/REVOKE block at the end of bootstrap, so every
--   step below is a guarded no-op (the reassignment loops simply find
--   nothing owned by riverbend_app to move).
--   Existing volume (this repo's shared local demo Postgres included):
--   riverbend_app is still the original bootstrap role and genuinely owns
--   audit_logs today — this migration performs the real transfer. It must
--   be run once via db/migrations/scripts/bootstrap_admin_role.sh using the
--   CURRENT DB_USER/DB_PASSWORD credential, because :'admin_user' does not
--   exist yet on that volume for db/migrations/apply.sh's normal
--   DB_ADMIN_USER-based connection to use. After that one-time run (either
--   path), apply.sh's ordinary connection works for every migration,
--   including re-running this one, which is then a no-op throughout.
--
-- ORDERING (do not silently demote before the transfer succeeds). Ownership
-- transfer and the runtime GRANTs happen BEFORE riverbend_app is stripped
-- of its elevated bits, and the whole file is one transaction — a failure
-- at any step rolls back everything, so riverbend_app is never left both
-- non-owner AND under-privileged, nor left superuser after a half-applied
-- grant set. Postgres role/ownership DDL (including CREATE ROLE and
-- per-object OWNER TO) is transactional, unlike some other databases, and
-- this was verified directly: a CREATE ROLE + ALTER ROLE inside
-- BEGIN/ROLLBACK leaves no trace.
--
-- :'admin_user' is granted SUPERUSER, matching what riverbend_app already
-- effectively has today as the original bootstrap role (this is a
-- relocation of existing privilege, not a new escalation) and avoiding
-- separately reasoning about the exact privilege set future schema changes
-- (CREATE EXTENSION, etc.) will need — see the migration header note above:
-- this is deliberately the minimum admin/runtime split, not a rebuild of
-- adr/0001's per-service least privilege.

BEGIN;

-- A server-side DO block with :'var' substitution used INSIDE it turned out
-- to be unreliable across multiple statements/lines in psql 15 (reproduced:
-- the FIRST :'var' reference in a dollar-quoted block substitutes fine, a
-- SECOND one does not, even on the same line) — so :'var'/:"var" are used
-- only in single, top-level statements below. Where a value needs to reach
-- PL/pgSQL, it is passed through a session GUC via SET + current_setting(),
-- which is ordinary SQL with no psql-side substitution involved.
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'admin_user') AS admin_role_exists \gset
\if :admin_role_exists
\echo 'admin role already exists, skipping creation'
\else
CREATE ROLE :"admin_user" WITH LOGIN PASSWORD :'admin_password' SUPERUSER;
\endif

SET riverbend_migration.admin_user = :'admin_user';

DO $$
DECLARE
    r RECORD;
    admin_role TEXT := current_setting('riverbend_migration.admin_user');
BEGIN
    FOR r IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public' AND tableowner = 'riverbend_app'
    LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO %I', r.tablename, admin_role);
    END LOOP;

    FOR r IN
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'S' AND n.nspname = 'public'
          AND pg_get_userbyid(c.relowner) = 'riverbend_app'
    LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO %I', r.relname, admin_role);
    END LOOP;

    FOR r IN
        SELECT p.oid::regprocedure::text AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) = 'riverbend_app'
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO %I', r.sig, admin_role);
    END LOOP;

    EXECUTE format('ALTER DATABASE %I OWNER TO %I', current_database(), admin_role);
END $$;

-- GRANT/REVOKE while riverbend_app is STILL superuser (on the
-- existing-volume path, the CONNECTED session is riverbend_app itself —
-- once it is demoted below, it no longer owns anything and is no longer a
-- superuser, so it could no longer grant privileges on tables it doesn't
-- own; superuser status is what makes self-granting possible here).
-- Demoting FIRST and granting after was tried and fails outright: "ERROR:
-- permission denied for table ..." on the very first ALL TABLES grant,
-- reproduced directly — which is exactly the ordering point 8 warns about.
GRANT USAGE ON SCHEMA public TO riverbend_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO riverbend_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO riverbend_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO riverbend_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"admin_user" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO riverbend_app;

-- audit_logs is the one deliberate exception: no UPDATE/DELETE for the
-- runtime role, regardless of the broad grant above.
REVOKE UPDATE, DELETE ON audit_logs FROM riverbend_app;

-- Demote LAST, once ownership has moved and every runtime grant is in
-- place — from this point riverbend_app can no longer ALTER TABLE
-- audit_logs at all (it owns nothing), which is what actually closes
-- AUD-B01. NOREPLICATION/NOBYPASSRLS strip two more attributes the
-- original bootstrap role carried (confirmed present on this cluster's
-- real riverbend_app) that a runtime DML role has no legitimate use for,
-- even though SUPERUSER alone is what point 6 names explicitly.
ALTER ROLE riverbend_app WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

COMMIT;
