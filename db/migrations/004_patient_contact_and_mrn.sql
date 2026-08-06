-- 004_patient_contact_and_mrn — richer demographics
-- 2026-02-18 · Helix Digital Partners
-- Added phone/email/gender/mrn + created_via to support self-service intake.
-- NOTE: mrn is captured but is NOT used as a duplicate-match key (RIV-160).

ALTER TABLE patients ADD COLUMN IF NOT EXISTS mrn         TEXT;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS gender      TEXT;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS phone       TEXT;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS email       TEXT;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS created_via TEXT;
