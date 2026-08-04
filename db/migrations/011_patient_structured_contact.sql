-- 011_patient_structured_contact — additive, transitional structured demographics
-- Week 6 UI update: Demographics & Contact intake screen now collects
-- first_name/last_name and a split address (street/city/state/zip_code)
-- instead of one combined name/address string.
--
-- This migration only ADDS nullable columns; it does not touch `name` or
-- `address`, which remain the legacy/composed columns every existing caller
-- (front-desk UI, records-service, reports) already reads. intake-service
-- (see services/intake-service/schemas.py Demographics) derives and writes
-- `name`/`address` from the structured fields when the caller supplies them,
-- so old and new callers both keep working against the same row shape.
--
-- zip_code is TEXT (not INTEGER/NUMERIC) to preserve leading zeros (e.g.
-- "02139") and ZIP+4 values (e.g. "02139-1234").
--
-- Rows written before this migration — and rows written by any caller that
-- still sends only the legacy `name`/`address` fields — will have NULL
-- first_name/last_name/city/state/zip_code. This is expected: there is no
-- reliable way to split a free-text legacy `name`/`address` back into these
-- structured parts, so no backfill is attempted here.

ALTER TABLE patients ADD COLUMN first_name TEXT;
ALTER TABLE patients ADD COLUMN last_name  TEXT;
ALTER TABLE patients ADD COLUMN city       TEXT;
ALTER TABLE patients ADD COLUMN state      TEXT;
ALTER TABLE patients ADD COLUMN zip_code   TEXT;
