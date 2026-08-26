#!/usr/bin/env bash
# docker-entrypoint-initdb.d runs this automatically, in filename order,
# ONLY when initializing a FRESH (empty) Postgres data volume — before
# 01-schema.sql. P3 admin/runtime role separation (w8-planner-2, AUD-B01):
# creates the riverbend_app runtime role up front, distinct from
# POSTGRES_USER (now the admin/bootstrap role — see docker-compose.yml).
#
# riverbend_app is LOGIN but NOT superuser/createdb/createrole from the
# moment it exists, and owns nothing — schema.sql (running as the admin
# role right after this) grants it exactly the runtime privileges it needs
# once the tables it needs to reference actually exist.
#
# For a volume that predates this split (riverbend_app already exists and
# is still the original bootstrap superuser), see
# db/migrations/028_admin_runtime_role_separation.sql and
# db/migrations/scripts/bootstrap_admin_role.sh instead — this script only
# ever runs on a brand new volume.
set -euo pipefail

: "${DB_APP_USER:?DB_APP_USER must be set}"
: "${DB_APP_PASSWORD:?DB_APP_PASSWORD must be set}"

# A server-side DO block with :'var' substitution inside it turned out to
# be unreliable across multiple statements/lines in psql 15 (reproduced:
# the FIRST :'var' in a dollar-quoted block substitutes fine, a SECOND one
# does not, even on the same line) — so this uses psql's own client-side
# \gset/\if instead, which only ever needs one :'var' substitution per
# plain top-level statement.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_user="$DB_APP_USER" -v app_password="$DB_APP_PASSWORD" <<-'EOSQL'
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') AS role_exists \gset
\if :role_exists
\echo 'riverbend_app already exists, skipping'
\else
CREATE ROLE :"app_user" WITH LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
\endif
EOSQL
