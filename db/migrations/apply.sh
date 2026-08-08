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
# records-service's SqlPatientAccessGate serves a chart read only when the
# patient has an ACTIVE grant: revoked_at IS NULL, expires_at NULL or in the
# future, AND the grant's user is still active. A plain migrate-and-restart
# against a database whose patients lack such a grant would silently deny those
# charts. This preflight uses the SAME active-grant predicate as the gate (so
# guard and gate can never disagree) and refuses to complete if ANY patient
# would be locked out, reporting how many — a table full of revoked/expired
# grants, or a partial backfill, no longer passes as "safe". Deploying
# intentionally closed/partial (Phase 1 of the two-phase rollout in
# docs/runbook.md) is still supported via RIVERBEND_ALLOW_PARTIAL_GRANTS=1,
# which acknowledges the remaining lockout explicitly.
if [ "${RIVERBEND_ALLOW_PARTIAL_GRANTS:-0}" = "1" ]; then
    echo "Preflight skipped (RIVERBEND_ALLOW_PARTIAL_GRANTS=1) — some patients may be ungranted (enforcement closed/partial)."
else
    echo "Preflight: verifying every patient has an active grant to an active user ..."
    unreachable=$(docker compose exec -T postgres psql -tA -v ON_ERROR_STOP=1 \
        -U "$DB_USER" -d "$DB_NAME" \
        -c "SELECT count(*) FROM patients p WHERE NOT EXISTS (
              SELECT 1 FROM patient_access_grants g JOIN users u ON u.id = g.user_id
              WHERE g.patient_id = p.id
                AND g.revoked_at IS NULL
                AND (g.expires_at IS NULL OR g.expires_at > now())
                AND u.is_active);" \
        | tr -d '[:space:]')
    if [ "${unreachable:-0}" != "0" ]; then
        echo "ERROR: ${unreachable} patient(s) have NO active grant to an active user." >&2
        echo "  Enabling grant enforcement now would deny every one of those charts" >&2
        echo "  — SqlPatientAccessGate honors only active grants (revoked_at IS NULL," >&2
        echo "  not expired, user still active), so revoked/expired/partial rows do" >&2
        echo "  not count. Backfill reviewed grants (docs/runbook.md 'Phase 2'), then" >&2
        echo "  re-run. For an intentional closed/partial rollout, re-run with" >&2
        echo "  RIVERBEND_ALLOW_PARTIAL_GRANTS=1." >&2
        exit 1
    fi
    echo "Preflight OK: every patient has an active grant to an active user."
fi
