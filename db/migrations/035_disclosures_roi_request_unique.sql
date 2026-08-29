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
--
-- Review finding ROI-MIG-001: unlike migration 013 (which fails loudly and
-- requires a separate, human-reviewed reconciliation script), a pre-existing
-- duplicate here is deterministic to resolve without guessing which row is
-- "real" — every disclosure the old, unlocked route ever produced for the
-- same request describes the SAME release, so keeping the earliest one
-- linked and unlinking the rest never invents or discards a fact. The block
-- below runs before the index, per partition of non-null roi_request_id,
-- ordered by disclosed_at ASC NULLS LAST then id ASC: the first row in that
-- order keeps its roi_request_id, every later row in the SAME partition has
-- it set to NULL. No row is deleted, and no other column is touched — the
-- full disclosure history (patient_id, authorization_id, disclosed_to,
-- disclosed_at, authorization_reference, purpose) survives on every row,
-- unlinked or not. A partition with only one row is untouched (row_number()
-- = 1 already).
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY roi_request_id
               ORDER BY disclosed_at ASC NULLS LAST, id ASC
           ) AS rn
    FROM disclosures
    WHERE roi_request_id IS NOT NULL
)
UPDATE disclosures d
SET roi_request_id = NULL
FROM ranked r
WHERE d.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS disclosures_roi_request_id_unique
    ON disclosures (roi_request_id) WHERE roi_request_id IS NOT NULL;
