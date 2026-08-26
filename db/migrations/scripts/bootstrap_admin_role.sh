#!/usr/bin/env bash
# One-time bootstrap for an EXISTING Postgres volume that predates admin/
# runtime role separation (028_admin_runtime_role_separation.sql, w8-planner-2
# P3 — closes AUD-B01).
#
# A FRESH volume never needs this: docker-compose.yml's postgres service
# boots with POSTGRES_USER=DB_ADMIN_USER, so that role exists from the
# container's own bootstrap and db/migrations/apply.sh's normal
# DB_ADMIN_USER-based connection works immediately.
#
# An EXISTING volume was bootstrapped before that change: only DB_USER
# (riverbend_app) exists, and — because it was the original POSTGRES_USER —
# it is still a full Postgres superuser on that volume today, which is
# exactly the gap 028 closes.
#
# Two steps, two different connecting roles:
#   1. Create the admin role (db/migrations/scripts/create_admin_role.sql),
#      connected as the CURRENT DB_USER credential — the only one that
#      exists yet on this volume, and still superuser here.
#   2. Run 028 (ownership transfer, grants, riverbend_app's demotion),
#      connected AS THE NEW ADMIN ROLE — not riverbend_app — so there is
#      no risk of the connecting session losing privilege partway through
#      its own transaction (see that migration's own ORDERING note).
#
# Neither step ever passes a password as a psql -v variable or command-line
# argument — both SQL files read DB_ADMIN_USER/DB_ADMIN_PASSWORD/DB_APP_PASSWORD
# via \getenv from the container's own environment (docker-compose.yml),
# never exec-time. This script's own equal-password check below is a plain
# bash string comparison of its OWN process variables — nothing is ever
# handed to another process's argv.
#
# Never reads, prints, or edits .env — configure DB_ADMIN_PASSWORD in your
# local environment (see .env.example) before running this.
#
# Idempotent: safe to re-run. Once the admin role exists and owns
# audit_logs, both steps are no-ops.
#
# Usage: db/migrations/scripts/bootstrap_admin_role.sh   (stack must be up)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DB_USER="${DB_USER:-riverbend_app}"
DB_NAME="${DB_NAME:-riverbend}"
DB_ADMIN_USER="${DB_ADMIN_USER:-riverbend_admin}"
DB_ADMIN_PASSWORD="${DB_ADMIN_PASSWORD:-}"
DB_PASSWORD="${DB_PASSWORD:-}"

cd "$REPO_ROOT"

if [ -z "$DB_ADMIN_PASSWORD" ]; then
    echo "DB_ADMIN_PASSWORD is not set." >&2
    echo "Configure a distinct value locally (see .env.example) before running this — never read or edited here." >&2
    exit 1
fi
if [ "$DB_ADMIN_PASSWORD" = "$DB_PASSWORD" ]; then
    echo "DB_ADMIN_PASSWORD must be distinct from DB_PASSWORD." >&2
    exit 1
fi

echo "Creating the admin role (if absent) ..."
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" \
    < db/migrations/scripts/create_admin_role.sql

echo "Applying 028 (ownership transfer, grants, demotion) as the admin role ..."
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$DB_ADMIN_USER" -d "$DB_NAME" \
    < db/migrations/028_admin_runtime_role_separation.sql

echo "Admin/runtime role separation applied. Use db/migrations/apply.sh (DB_ADMIN_USER-based) for every future migration."
