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

# PR #22 review — patient-grant rollout guard (Week 4 catch-up, RIV-201).
# records-service's SqlPatientAccessGate requires a patient_access_grants row
# per chart read, and migration 014 ships that table empty. A plain
# migrate-and-restart against a database that already has patients would
# therefore silently deny EVERY existing chart until someone backfills grants
# out-of-band. This preflight makes that cutover fail-closed AND loud: it
# refuses to complete unless grants are populated (or there are no patients to
# lock out yet). To deploy intentionally "closed" — Phase 1 of the two-phase
# rollout in docs/runbook.md, grants backfilled in Phase 2 — re-run with
# RIVERBEND_ALLOW_EMPTY_GRANTS=1 to acknowledge the lockout explicitly.
if [ "${RIVERBEND_ALLOW_EMPTY_GRANTS:-0}" = "1" ]; then
    echo "Preflight skipped (RIVERBEND_ALLOW_EMPTY_GRANTS=1) — deploying with grant enforcement CLOSED."
else
    echo "Preflight: verifying patient_access_grants is populated when patients exist ..."
    locked_out=$(docker compose exec -T postgres psql -tA -v ON_ERROR_STOP=1 \
        -U "$DB_USER" -d "$DB_NAME" \
        -c "SELECT (SELECT count(*) FROM patients) > 0 AND (SELECT count(*) FROM patient_access_grants) = 0;" \
        | tr -d '[:space:]')
    if [ "$locked_out" = "t" ]; then
        echo "ERROR: patients exist but patient_access_grants is EMPTY." >&2
        echo "  Enabling grant enforcement now would deny ALL existing chart access" >&2
        echo "  (records-service SqlPatientAccessGate requires a grant per read)." >&2
        echo "  Populate reviewed grants first — see docs/runbook.md 'Phase 2' —" >&2
        echo "  then re-run. To deploy intentionally closed (Phase 1), re-run with" >&2
        echo "  RIVERBEND_ALLOW_EMPTY_GRANTS=1." >&2
        exit 1
    fi
    echo "Preflight OK: grants populated (or no patients yet)."
fi
