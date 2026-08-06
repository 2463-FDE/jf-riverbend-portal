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
-- Reconciling existing dirty data before the slot index: this repo's own
-- seed data (db/seed/generate_seed.py) already has multiple slots with
-- more than one 'confirmed' appointment — one is the hand-authored RIV-175
-- teaching fixture (slot 88231, two confirmed rows ~400ms apart), the rest
-- are an unintended side effect of the seed generator's random slot
-- assignment never checking for a collision. A plain CREATE UNIQUE INDEX
-- would fail outright against this data — the same lesson migration 009
-- already learned for insurance_coverages.status. Never silently delete
-- the loser: for each slot_id, the earliest-created 'confirmed' row
-- (created_at, ties broken by id) stays confirmed; every other confirmed
-- row for that slot is reclassified to 'cancelled_duplicate' and stamped
-- with reconciled_duplicate_of pointing at the id of the row it lost to —
-- fully recoverable/auditable, nothing destroyed. Guarded by the same
-- pg_indexes check as the index creation itself, so this reconciliation
-- runs exactly once, not on every apply.sh re-run.
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reconciled_duplicate_of INTEGER REFERENCES appointments(id);

DO $$
DECLARE
    reconciled_count integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'appointments_confirmed_slot_unique'
    ) THEN
        WITH ranked AS (
            SELECT id, slot_id,
                   row_number() OVER (
                       PARTITION BY slot_id
                       ORDER BY created_at ASC, id ASC
                   ) AS rn
            FROM appointments
            WHERE status = 'confirmed'
        ),
        losers AS (
            SELECT r.id, first_row.id AS winner_id
            FROM ranked r
            JOIN ranked first_row ON first_row.slot_id = r.slot_id AND first_row.rn = 1
            WHERE r.rn > 1
        )
        UPDATE appointments a
            SET status = 'cancelled_duplicate',
                reconciled_duplicate_of = losers.winner_id
            FROM losers
            WHERE a.id = losers.id;

        GET DIAGNOSTICS reconciled_count = ROW_COUNT;
        IF reconciled_count > 0 THEN
            RAISE NOTICE 'appointments_confirmed_slot_unique: reconciled % pre-existing duplicate-confirmed appointment(s) to cancelled_duplicate before adding the unique index; see reconciled_duplicate_of for the surviving appointment each was superseded by.', reconciled_count;
        END IF;

        CREATE UNIQUE INDEX appointments_confirmed_slot_unique
            ON appointments(slot_id) WHERE status = 'confirmed';
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS appointments_idempotency_key_unique
    ON appointments(patient_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
