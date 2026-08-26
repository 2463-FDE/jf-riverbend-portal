#!/usr/bin/env bash
# Runs once, automatically, when docker-entrypoint-initdb.d initializes a
# FRESH (empty) Postgres data volume — before 01-schema.sql. Creates the
# riverbend_app runtime role: LOGIN, but NOT owner of anything and NOT
# superuser/createdb/createrole from the start (unlike the old single-role
# design, where the app credential WAS the bootstrap superuser). schema.sql
# grants it runtime privileges after creating the tables it needs to exist
# first. See db/migrations/028_admin_runtime_role_separation.sql for the
# equivalent transition on a volume that predates this split.
set -euo pipefail

: "${DB_APP_USER:?DB_APP_USER must be set}"
: "${DB_APP_PASSWORD:?DB_APP_PASSWORD must be set}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD (the admin role password) must be set}"

# Round 2 review: a shared admin/runtime secret would let anyone who knows
# the runtime credential authenticate as the admin role too, defeating the
# whole privilege split — fail the container's own initialization outright
# rather than boot with that hole. Bash string comparison only — neither
# value is ever passed to another process's argv.
if [ "$POSTGRES_PASSWORD" = "$DB_APP_PASSWORD" ]; then
    echo "FATAL: DB_ADMIN_PASSWORD (POSTGRES_PASSWORD) must be distinct from DB_PASSWORD (DB_APP_PASSWORD)." >&2
    exit 1
fi

# A server-side DO block with :'var' substitution inside it turned out to
# be unreliable across multiple statements/lines in psql 15 (reproduced:
# the FIRST :'var' in a dollar-quoted block substitutes fine, a SECOND one
# does not, even on the same line) — so this uses psql's own client-side
# \gset/\if instead, which only ever needs one :'var' substitution per
# plain top-level statement. \getenv reads DB_APP_USER/DB_APP_PASSWORD
# directly from this script's own process environment (already set by
# docker-compose.yml, not passed as a psql -v argument or CLI flag), so
# neither ever appears in `ps`, shell history, or a process log.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'EOSQL'
\getenv app_user DB_APP_USER
\getenv app_password DB_APP_PASSWORD
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') AS role_exists \gset
\if :role_exists
\echo 'riverbend_app already exists, skipping'
\else
CREATE ROLE :"app_user" WITH LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
\endif
EOSQL
