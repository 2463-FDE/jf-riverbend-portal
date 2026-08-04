-- 007_encounter_record_detail — clinical detail for the records viewer
-- 2026-04-07 · Helix Digital Partners
-- Adds reason/location/status to encounters and title/status/reference_range to
-- records so the portal can show lab results with reference ranges.
-- NOTE: still no index on records.body — /records/search is a full table scan.

ALTER TABLE encounters ADD COLUMN IF NOT EXISTS reason   TEXT;
ALTER TABLE encounters ADD COLUMN IF NOT EXISTS location TEXT;
ALTER TABLE encounters ADD COLUMN IF NOT EXISTS status   TEXT DEFAULT 'finished';

ALTER TABLE records ADD COLUMN IF NOT EXISTS title           TEXT;
ALTER TABLE records ADD COLUMN IF NOT EXISTS status          TEXT;
ALTER TABLE records ADD COLUMN IF NOT EXISTS reference_range TEXT;
