#!/usr/bin/env bash
# Runs schema.sql via an explicit psql invocation, in place of
# docker-entrypoint-initdb.d's own built-in handling for a plain .sql file
# (a fixed `psql -f` call with no \getenv/-v support at all). schema.sql's
# runtime-role grant block (round-3 review, P3 w8-planner-2) reads the
# runtime role's NAME via \getenv DB_APP_USER — never hard-coded, never a
# command-line argument — which only works when psql itself reads the file
# as a script; mounting schema.sql directly at a NN-name.sql path in
# docker-entrypoint-initdb.d would bypass that entirely. schema.sql itself
# is instead mounted at /docker-entrypoint-initdb.d-src/schema.sql (outside
# docker-entrypoint-initdb.d, so it is never auto-executed a second time) —
# see docker-compose.yml.
#
# Fails clearly if DB_APP_USER is absent, same shape as
# db/docker-init/00-create-app-role.sh's own guard.
set -euo pipefail

: "${DB_APP_USER:?DB_APP_USER must be set — schema.sql grants runtime privileges to this role by name}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -f /docker-entrypoint-initdb.d-src/schema.sql
