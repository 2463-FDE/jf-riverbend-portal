-- create_admin_role.sql — one-time admin-role bootstrap for a Postgres
-- volume that predates admin/runtime role separation (P3 w8-planner-2,
-- AUD-B01/round-2 review). Run by db/migrations/scripts/bootstrap_admin_role.sh
-- ONLY, connected as the current DB_USER credential (still a superuser on
-- such a volume — see that script). A fresh volume never needs this file:
-- docker-compose.yml's postgres service already boots with
-- POSTGRES_USER=DB_ADMIN_USER, so the admin role exists from the
-- container's own bootstrap.
--
-- 028_admin_runtime_role_separation.sql ASSUMES the admin role already
-- exists — it never creates one and never touches a password — so this is
-- the only file in the whole migration/bootstrap chain that sets a
-- password, and the only one that needs to run before the admin role can
-- be used at all.
--
-- DB_ADMIN_USER/DB_ADMIN_PASSWORD/DB_PASSWORD are read via \getenv from
-- this script's own process environment (set by docker-compose.yml, never
-- passed as a psql -v variable or command-line argument) — neither
-- password is ever visible in `ps`, shell history, or a process log.
--
-- Refuses to create the admin role if its password equals DB_PASSWORD —
-- a shared secret would let anyone who knows the runtime credential
-- authenticate as the admin role too, defeating the whole point of this
-- split. Idempotent otherwise: a no-op if the admin role already exists.

\getenv admin_user DB_ADMIN_USER
\getenv admin_password DB_ADMIN_PASSWORD
\getenv app_password DB_PASSWORD

SELECT (:'admin_password' = :'app_password') AS passwords_equal \gset
\if :passwords_equal
DO $$ BEGIN RAISE EXCEPTION 'DB_ADMIN_PASSWORD must be distinct from DB_PASSWORD'; END $$;
\endif

SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'admin_user') AS admin_role_exists \gset
\if :admin_role_exists
\echo 'admin role already exists, skipping creation'
\else
CREATE ROLE :"admin_user" WITH LOGIN PASSWORD :'admin_password' SUPERUSER;
\endif
