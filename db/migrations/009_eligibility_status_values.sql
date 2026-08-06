-- 009_eligibility_status_values — widen insurance_coverages.status vocabulary
-- 2026-07-17 · Stage 1 resilience fix (D4 / RIV-088 / RIV-141)
-- Formalizes the status column with an explicit CHECK constraint covering the
-- eligibility-service EligibilityStatus contract (active | inactive | unknown
-- | pending | stale). Previously an unconstrained TEXT column (comment-only
-- convention, no CHECK). 'pending' and 'stale' are new values a later stage
-- will start writing once eligibility moves off the synchronous intake path
-- (RIV-088, async job status) and once a last-known-good cache fallback
-- during a payer outage needs to be reflected here (RIV-141).
-- Existing rows are unaffected: 'active' | 'inactive' | 'unknown' (the only
-- values ever written so far — see db/seed/generate_seed.py) all satisfy the
-- new constraint.

-- Postgres has no `ADD CONSTRAINT IF NOT EXISTS`; guard with a DO block so
-- this is safe to re-run (see db/migrations/apply.sh).
--
-- PR #20 review (round 5, data-safety): adding a CHECK constraint (unlike
-- IF NOT EXISTS DDL) scans and validates every EXISTING row — on a real,
-- possibly-drifted clinic database (manual repairs, an old bug, direct SQL
-- surgery — this repo's own runbook documents multiple "reconcile/cancel
-- manually" workarounds), a row with an out-of-vocabulary status would make
-- the ALTER TABLE fail, and apply.sh's `set -e` would then strand the
-- deploy before 010/011 run.
--
-- A round-3 fix for that blindly overwrote any such value with 'unknown'
-- with no audit trail — trading a failed deploy for silent, irreversible
-- loss of real eligibility/billing state. This is the corrected version:
-- the original value is preserved in the new status_legacy column before
-- being remapped, and a NOTICE is raised so the remap is visible in deploy
-- output, not silent. Nothing is destroyed; a human can review
-- status_legacy afterward and decide whether any row needs real repair.
--
-- Caveat: this only protects future applications of this migration. It
-- cannot recover a value already overwritten by an earlier run of the
-- unfixed (round-3) version of this file — this branch has never been
-- deployed, so that is a theoretical gap here, not a real incident.
ALTER TABLE insurance_coverages ADD COLUMN IF NOT EXISTS status_legacy TEXT;

DO $$
DECLARE
    drifted_count integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'insurance_coverages_status_check'
    ) THEN
        SELECT count(*) INTO drifted_count
        FROM insurance_coverages
        WHERE status IS NOT NULL
          AND status NOT IN ('active', 'inactive', 'unknown', 'pending', 'stale');

        IF drifted_count > 0 THEN
            RAISE NOTICE 'insurance_coverages_status_check: remapping % row(s) with an out-of-vocabulary status to ''unknown''; original value preserved in status_legacy for review.', drifted_count;
        END IF;

        UPDATE insurance_coverages
            SET status_legacy = status,
                status = 'unknown'
            WHERE status IS NOT NULL
              AND status NOT IN ('active', 'inactive', 'unknown', 'pending', 'stale');

        ALTER TABLE insurance_coverages
            ADD CONSTRAINT insurance_coverages_status_check
            CHECK (status IN ('active', 'inactive', 'unknown', 'pending', 'stale'));
    END IF;
END
$$;
