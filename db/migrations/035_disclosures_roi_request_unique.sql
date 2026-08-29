-- 035_disclosures_roi_request_unique — W10 Final Stage 2.
--
-- fulfill_roi_request (services/roi-service/app.py) checked-then-inserted
-- with no database-level guard: two simultaneous fulfillment attempts for
-- the SAME roi_request_id could both read status != 'fulfilled' before
-- either committed, and both insert a disclosure row — a real, patient-
-- facing duplicate release, not just a duplicate log entry. The route now
-- takes a `SELECT ... FOR UPDATE` row lock on the request first (closing
-- the race in the normal case); this index is the database-level backstop
-- that makes a second disclosure for the same request impossible even if
-- something else ever bypasses that lock.
--
-- Partial (not a plain UNIQUE), same shape as migration 013's confirmed-
-- slot index: disclosures.roi_request_id is nullable in principle (a
-- disclosure not tied to a specific tracked request), so only rows that
-- DO reference a request are constrained to at most one each.

CREATE UNIQUE INDEX IF NOT EXISTS disclosures_roi_request_id_unique
    ON disclosures (roi_request_id) WHERE roi_request_id IS NOT NULL;
