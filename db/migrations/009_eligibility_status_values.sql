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

ALTER TABLE insurance_coverages
    ADD CONSTRAINT insurance_coverages_status_check
    CHECK (status IN ('active', 'inactive', 'unknown', 'pending', 'stale'));
