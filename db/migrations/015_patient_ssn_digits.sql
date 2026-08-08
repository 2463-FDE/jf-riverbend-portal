-- 015_patient_ssn_digits — indexed, database-computed SSN match key
-- (Week 4 catch-up, PR #22 review round 5 — medium).
--
-- services/records-service/reconciliation.py::find_ssn_match_ids previously
-- read every patient's ssn into Python and normalized+compared row by row on
-- EVERY reconciliation request — an unbounded full-table scan reading PHI
-- (other patients' SSNs) outside the caller's authorized scope, and a
-- timeout risk at real patient volume.
--
-- ssn_digits is a STORED, database-computed generated column: pure digit
-- extraction, no validation of SSA-invalid patterns (never-issued areas,
-- repeated digits, etc.) — that validation stays in Python
-- (reconciliation.py::_normalize_ssn) and is applied to the QUERY key before
-- it is ever used as a lookup value, so a stored placeholder like
-- "000-00-0000" (ssn_digits='000000000') can never equal a validated key —
-- see find_ssn_match_ids for the reasoning.
--
-- Generated columns are computed automatically on INSERT/UPDATE, including
-- for pre-existing rows as part of this ALTER TABLE — no backfill step, and
-- no change needed in intake-service or the seed generator to populate it.

ALTER TABLE patients ADD COLUMN IF NOT EXISTS ssn_digits TEXT
    GENERATED ALWAYS AS (regexp_replace(ssn, '\D', '', 'g')) STORED;

CREATE INDEX IF NOT EXISTS patients_ssn_digits_idx ON patients (ssn_digits);
