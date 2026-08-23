-- 023_coverage_verification_job — Coverage & Eligibility workspace (W9.3).
--
-- eligibility-service's own job record (jobs.py) deliberately stores no
-- patient_id/coverage_id — it is a minimized-payload design, keyed only on
-- an opaque job_id and the member_id string it was created with. That is
-- correct for what it is, but it means nothing upstream can safely say
-- "the current verification job FOR THIS COVERAGE" without somewhere to
-- keep that association — and the raw job_id must never reach the browser
-- (see gateway's coverage/eligibility routes: an arbitrary job_id is not an
-- authorization boundary). This column is that association, held by the
-- one row it is actually about.
ALTER TABLE insurance_coverages
    ADD COLUMN IF NOT EXISTS verification_job_id TEXT;
