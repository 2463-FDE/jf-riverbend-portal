#!/usr/bin/env bash
# check_grant_coverage.sh — OPT-IN grant-coverage validation (Week 4 catch-up,
# RIV-201). Deliberately NOT run by db/migrations/apply.sh.
#
# PR #22 review round 4 (2026-08-08): an earlier version of this check ran
# unconditionally, post-migration, inside apply.sh. Three problems with that:
#   1. It ran AFTER migrations were applied, so a failure left the schema
#      already mutated while the command reported non-zero — a brittle,
#      non-idempotent-looking deploy path.
#   2. The committed seed has 255 patients but only 7 grant rows (front-desk/
#      provider demo grants — see db/seed/generate_seed.py), so the guard
#      failed on every normal `make seed` + apply.sh run in dev.
#   3. It was opt-OUT (RIVERBEND_ALLOW_PARTIAL_GRANTS=1 to skip), so routine
#      deploy automation for an intentionally partial/Phase-1 rollout
#      (docs/runbook.md) was blocked unless an operator knew to set it.
#
# Fix: this check is now a separate, explicit, OPT-IN script. Run it only when
# you are about to rely on grant enforcement in an environment with real
# existing patients — i.e. before promoting past Phase 1 of the two-phase
# rollout, not as part of every migrate/seed/deploy.
#
# Usage: db/migrations/scripts/check_grant_coverage.sh
# Exit 0 if every patient has an active grant to an active user; exit 1 with
# the unreachable count otherwise. Uses the EXACT active-grant predicate
# services/records-service/patient_access_gate.py's SqlPatientAccessGate
# checks (revoked_at IS NULL, expires_at NULL or future, user is_active), so
# this check and the gate can never disagree — a table of revoked/expired
# grants or a partial backfill does not count as coverage.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DB_USER="${DB_USER:-riverbend_app}"
DB_NAME="${DB_NAME:-riverbend}"

cd "$REPO_ROOT"

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
    echo "NOT READY: ${unreachable} patient(s) have NO active grant to an active user." >&2
    echo "  Enforcing the patient-access gate now would deny every one of those" >&2
    echo "  charts — revoked/expired/partial grant rows do not count as coverage." >&2
    echo "  Backfill reviewed grants (docs/runbook.md 'Phase 2') and re-run." >&2
    exit 1
fi

echo "OK: every patient has an active grant to an active user."
