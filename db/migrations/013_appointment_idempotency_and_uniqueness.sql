-- 013_appointment_idempotency_and_uniqueness — close the RIV-175 double-booking race
-- 2026-08-06 · Stage 4 (Week 5) — Booking Correctness and Release
--
-- book.py's classic check-then-insert race (RIV-175, "charged twice" / two
-- confirmations for one appointment) has no DB-level guard today:
-- appointments.slot_id has no UNIQUE constraint at all (see db/schema.sql's
-- own "NOTE: no UNIQUE constraint, no FK" comment) — two near-simultaneous
-- requests for the same slot can both pass a read-check and both insert a
-- 'confirmed' row. This migration adds the two guards that actually close
-- the race at the database level, not in application code:
--
--   1. A partial UNIQUE index on (slot_id) WHERE status = 'confirmed' — at
--      most one confirmed appointment per slot, enforced by Postgres
--      itself. Partial (not a plain UNIQUE) so a slot can be legitimately
--      rebooked after its one confirmed appointment is cancelled.
--   2. A per-actor idempotency key: idempotency_key (nullable — no
--      existing row ever had one) plus a partial UNIQUE index on
--      (patient_id, idempotency_key) WHERE idempotency_key IS NOT NULL, so
--      a client retry of a slow POST with the same key returns the
--      original booking instead of racing a second insert for it.
--
-- Round-22 review (2026-08-06): this migration originally reconciled
-- pre-existing duplicate-confirmed rows ITSELF — ranking by created_at/id
-- and auto-flipping every loser to 'cancelled_duplicate' before adding the
-- index, the same "remap and log a NOTICE" shape migration 009 already
-- used for insurance_coverages.status. The review correctly called that
-- out as too consequential to do silently in a migration: unlike remapping
-- an out-of-vocabulary status value (009's case, already-anomalous data
-- with no valid interpretation), flipping 'confirmed' to
-- 'cancelled_duplicate' is an application-visible, patient-facing state
-- change based only on a created_at/id heuristic, with no human review —
-- on real production data that can silently cancel a real appointment
-- someone is counting on.
--
-- This migration now FAILS LOUDLY instead and touches zero rows if any
-- slot still has more than one confirmed appointment. Resolving those
-- duplicates is a separate, explicit, human-reviewed step — see
-- db/migrations/scripts/reconcile_duplicate_confirmed_appointments.sql and
-- docs/runbook.md's "Before applying migration 013" section. Run that
-- script (reviewed by a human, not just an engineer) first, then re-run
-- this migration; its preflight will pass once no slot has more than one
-- confirmed appointment left.
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reconciled_duplicate_of INTEGER REFERENCES appointments(id);

DO $$
DECLARE
    dup_slot_count integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'appointments_confirmed_slot_unique'
    ) THEN
        SELECT count(*) INTO dup_slot_count FROM (
            SELECT slot_id FROM appointments
            WHERE status = 'confirmed'
            GROUP BY slot_id
            HAVING count(*) > 1
        ) dupes;

        IF dup_slot_count > 0 THEN
            RAISE EXCEPTION 'appointments_confirmed_slot_unique: % slot(s) still have more than one confirmed appointment. This migration will NOT auto-cancel a real confirmed appointment based on created_at/id alone. Run db/migrations/scripts/reconcile_duplicate_confirmed_appointments.sql (reviewed by a human first) to resolve them, then re-run this migration. See docs/runbook.md.', dup_slot_count;
        END IF;

        CREATE UNIQUE INDEX appointments_confirmed_slot_unique
            ON appointments(slot_id) WHERE status = 'confirmed';
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS appointments_idempotency_key_unique
    ON appointments(patient_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
