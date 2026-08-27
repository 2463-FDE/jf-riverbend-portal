-- 031_phi_field_encryption.sql — w8-planner-2 P2 (adr/0012)
--
-- Converts patients.ssn_digits from a Postgres GENERATED column (computed
-- server-side from plaintext ssn, migration 015) into a plain,
-- application-populated column. Once ssn is application-encrypted, the
-- GENERATED expression is meaningless — regexp_replace over AEAD
-- ciphertext extracts nothing usable — so the column becomes an ordinary
-- TEXT column that intake-service/records-service populate themselves with
-- an HMAC-SHA256 blind index (libs/phi_crypto.compute_blind_index), NOT
-- raw digits. The existing index (patients_ssn_digits_idx) and the
-- equality query in reconciliation.py both keep working unchanged — only
-- what's actually stored in the column changes.
--
-- This migration does NOT itself encrypt any row's ssn/dob/notes, and does
-- NOT recompute ssn_digits for existing rows — a SQL-only migration has no
-- way to reach PHI_ENCRYPTION_KEY_V1/PHI_BLIND_INDEX_KEY_V1. That is
-- db/migrations/scripts/encrypt_existing_phi.py's job: a one-time,
-- operator-invoked backfill, not part of apply.sh's automatic idempotent-
-- DDL sweep. Deploy order: this migration, then the backfill script, then
-- the new intake-service/records-service code — see adr/0012 for why
-- (deploying the new code first makes records-service try to AEAD-decrypt
-- still-plaintext rows and fail closed on every one).

ALTER TABLE patients ALTER COLUMN ssn_digits DROP EXPRESSION IF EXISTS;

-- Which PHI_ENCRYPTION_KEY_V<n> (and, for ssn, which PHI_BLIND_INDEX_KEY_V<n>
-- — the two share one version identifier, see adr/0012) encrypted this
-- field. NULL alongside a non-NULL ssn/dob/notes means the row predates
-- this migration's backfill and is still plaintext — intentionally NOT
-- backfilled by this file itself; see above.
ALTER TABLE patients ADD COLUMN IF NOT EXISTS ssn_key_version   TEXT;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS dob_key_version   TEXT;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS notes_key_version TEXT;

COMMENT ON COLUMN patients.ssn IS
    'AEAD-encrypted (libs/phi_crypto) once ssn_key_version is set on this row; see encrypt_existing_phi.py for the backfill of rows written before migration 031.';
COMMENT ON COLUMN patients.dob IS
    'AEAD-encrypted (libs/phi_crypto) once dob_key_version is set on this row.';
COMMENT ON COLUMN patients.notes IS
    'AEAD-encrypted (libs/phi_crypto) once notes_key_version is set on this row.';
COMMENT ON COLUMN patients.ssn_digits IS
    'HMAC-SHA256 blind index (libs/phi_crypto.compute_blind_index) keyed by ssn_key_version''s PHI_BLIND_INDEX_KEY_V<n> — NOT raw digits since migration 031. Deterministic exact-match key for reconciliation.py; not reversible without that key.';
