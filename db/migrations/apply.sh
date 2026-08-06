#!/usr/bin/env bash
# Apply every db/migrations/*.sql file, in order, against the running
# `postgres` compose service — Week 1 catch-up deploy-safety fix.
#
# Safe to run repeatedly and against a database at ANY prior migration
# point: every file in this directory uses IF NOT EXISTS / guarded DDL, so
# re-applying an already-applied migration is a no-op, not an error.
#
# Why not a migration-tracking table instead: existing per-clinic
# deployments were initialized once from whatever version of db/schema.sql
# existed at the time (docker-entrypoint-initdb.d only runs on a fresh,
# empty Postgres volume) and have never run an incremental migration since
# — there is no recorded history to bootstrap a tracking table against.
# Idempotent-by-construction migrations sidestep that unknown instead of
# guessing it. See docs/runbook.md "Deploying a new release."
#
# Usage: db/migrations/apply.sh   (run from anywhere; the stack must be up)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB_USER="${DB_USER:-riverbend_app}"
DB_NAME="${DB_NAME:-riverbend}"

cd "$REPO_ROOT"

for f in db/migrations/*.sql; do
    echo "Applying $(basename "$f") ..."
    docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" < "$f"
done

echo "All migrations applied (already-applied ones were no-ops)."
