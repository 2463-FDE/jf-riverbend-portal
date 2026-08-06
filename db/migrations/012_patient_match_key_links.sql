-- 012_patient_match_key_links — non-destructive identity-link audit trail
-- Week 2-3 catch-up: implements the linking mechanism proposed (not
-- implemented) by adr/0004-master-patient-index-match-key.md for AUD-09 /
-- RIV-160 (Maria Gonzalez exists as 3 separate patient rows with no shared
-- key — see db/seed/generate_seed.py).
--
-- This does NOT retroactively merge or backfill any existing duplicate —
-- adr/0004 explicitly scopes that out ("No retroactive merge of existing
-- duplicates"). It only gives intake-service (services/intake-service/
-- app.py::_find_match_candidates) somewhere to record a link going forward,
-- confirmed or not, so a duplicate is reviewable instead of a silent new
-- fragment.
--
-- confidence:
--   'exact'   — dob + full ssn agreed (adr/0004 "certain duplicate")
--   'partial' — ssn agreed, dob (or other fields) did not
--     (adr/0004 "possible duplicate", staff review, never auto-merged)
-- confirmed=false means a partial match recorded for review, or an exact
-- match nobody has acted on yet; confirmed=true means a staff decision
-- (confirmed_by) was recorded — either "yes, link/treat as the same person"
-- or "no, but note the resemblance" depending on how intake-service used it
-- (see IntakeRequest.duplicate_override).

CREATE TABLE IF NOT EXISTS patient_links (
    id                SERIAL PRIMARY KEY,
    patient_id        INTEGER NOT NULL REFERENCES patients(id),
    linked_patient_id INTEGER NOT NULL REFERENCES patients(id),
    confidence        TEXT NOT NULL CHECK (confidence IN ('exact', 'partial')),
    basis             TEXT,             -- coded reason, e.g. "ssn_dob_match" — never raw PHI values
    confirmed         BOOLEAN NOT NULL DEFAULT FALSE,
    confirmed_by      TEXT,             -- staff identifier, NULL until confirmed (see known-limitation note in app.py)
    confirmed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (patient_id <> linked_patient_id)
);

CREATE INDEX IF NOT EXISTS patient_links_patient_id_idx ON patient_links (patient_id);
CREATE INDEX IF NOT EXISTS patient_links_linked_patient_id_idx ON patient_links (linked_patient_id);
