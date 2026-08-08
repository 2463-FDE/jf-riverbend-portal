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

# This runner only applies SCHEMA — it never blocks on data state, so a routine
# deploy, an intentional partial/Phase-1 rollout, and `make seed` in dev all
# complete cleanly (PR #22 review: don't fail a deploy after already mutating
# the schema, and don't reject the seeded/demo state).
#
# Grant-coverage validation is a SEPARATE, OPT-IN step, not part of this
# unconditional runner. Before ENABLING grant enforcement in production, verify
# every existing patient has an active grant:
#   db/migrations/scripts/check_grant_coverage.sh
# records-service's gate honors only active grants, so a table of revoked/
# expired grants or a partial backfill would still deny those charts — see
# docs/runbook.md "Phase 2".
