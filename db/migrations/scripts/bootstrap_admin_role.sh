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
# exactly the gap 028 closes. This script runs THAT ONE migration using the
# current DB_USER/DB_PASSWORD credential (the only one that exists yet),
# creating DB_ADMIN_USER and demoting riverbend_app in the same transaction.
# Idempotent: safe to re-run, and a no-op once riverbend_admin already
# exists and owns audit_logs.
#
# After this has run once, switch to the normal `db/migrations/apply.sh` for
# every future migration (including re-running 028 itself, harmlessly).
#
# Usage: db/migrations/scripts/bootstrap_admin_role.sh   (stack must be up)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DB_USER="${DB_USER:-riverbend_app}"
DB_NAME="${DB_NAME:-riverbend}"
DB_ADMIN_USER="${DB_ADMIN_USER:-riverbend_admin}"
DB_ADMIN_PASSWORD="${DB_ADMIN_PASSWORD:-${DB_PASSWORD:-}}"

cd "$REPO_ROOT"

docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
    -v admin_user="$DB_ADMIN_USER" -v admin_password="$DB_ADMIN_PASSWORD" \
    -U "$DB_USER" -d "$DB_NAME" \
    < db/migrations/028_admin_runtime_role_separation.sql

echo "Admin/runtime role separation applied. Use db/migrations/apply.sh (now DB_ADMIN_USER-based) for every future migration."
