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
-- ASSUMES THE ADMIN ROLE ALREADY EXISTS (round 2 review). This migration
-- never creates a role and never touches a password — it only transfers
-- ownership, grants, and demotes. On a fresh volume, docker-compose.yml's
-- postgres service boots with POSTGRES_USER=DB_ADMIN_USER, so the role
-- exists from the container's own bootstrap. On an existing volume, run
-- db/migrations/scripts/bootstrap_admin_role.sh once first — it creates
-- the admin role (db/migrations/scripts/create_admin_role.sql) and is the
-- only file in this whole chain that ever sets a password. This migration
-- reads only the admin role's NAME (:'admin_user', via \getenv — never a
-- password, and never a psql -v argument either), so db/migrations/apply.sh
-- can run it exactly like every other migration file.
--
-- WHAT THIS DOES. Transfers ownership of every table, sequence, and
-- function riverbend_app currently owns in the public schema (plus the
-- database itself) to the admin role, grants riverbend_app exactly the
-- runtime privileges it needs — full CRUD on ordinary tables, but only
-- INSERT + SELECT on audit_logs — then strips riverbend_app's superuser/
-- createdb/createrole bits. From this point riverbend_app cannot ALTER
-- TABLE audit_logs at all (that requires ownership), which is what
-- actually closes AUD-B01.
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
--   Fresh volume: the reassignment loops below simply find nothing owned
--   by riverbend_app to move (schema.sql's own tail grant block already
--   applied the same GRANT/REVOKE at bootstrap), so this is a no-op.
--   Existing volume (this repo's shared local demo Postgres included):
--   riverbend_app is still the original bootstrap role and genuinely owns
--   audit_logs today — this migration performs the real transfer, run via
--   bootstrap_admin_role.sh connected AS THE ADMIN ROLE (not riverbend_app
--   — see that script), so there is no risk of the connecting session
--   losing privilege partway through its own transaction.
--
-- ORDERING. Ownership transfer and the runtime GRANTs happen before
-- riverbend_app is stripped of its elevated bits, and the whole file is
-- one transaction — a failure at any step rolls back everything, so
-- riverbend_app is never left both non-owner AND under-privileged.
-- (An earlier revision of this migration ran AS riverbend_app itself and
-- demoted before granting, which failed outright — "ERROR: permission
-- denied for table ..." — the moment the now-demoted, now-non-owner
-- session tried to GRANT something it no longer had authority over.
-- Running this as the admin role instead removes that hazard entirely,
-- but the ordering is kept as good practice regardless.)

BEGIN;

\getenv admin_user DB_ADMIN_USER

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

-- NOREPLICATION/NOBYPASSRLS strip two more attributes the original
-- bootstrap role carried (confirmed present on this cluster's real
-- riverbend_app) that a runtime DML role has no legitimate use for, even
-- though SUPERUSER alone is what point 6 names explicitly.
ALTER ROLE riverbend_app WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

COMMIT;
