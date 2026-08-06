-- reconcile_duplicate_confirmed_appointments.sql
-- Round-22 review (2026-08-06, PR #20): explicit, human-run reconciliation
-- for pre-existing duplicate-confirmed appointments. NOT a numbered
-- migration — this lives under scripts/ specifically so
-- db/migrations/apply.sh's `for f in db/migrations/*.sql` glob never picks
-- it up and runs it automatically. migration 013's own preflight refuses
-- to create appointments_confirmed_slot_unique while any slot still has
-- more than one 'confirmed' appointment, precisely so resolving that
-- requires a deliberate decision instead of happening silently during a
-- deploy.
--
-- STOP: run the SELECT below FIRST and have a human (clinical
-- ops/billing, not just an engineer) review the list before running the
-- UPDATE beneath it. Changing an appointment from 'confirmed' to
-- 'cancelled_duplicate' is a real, patient-facing state change, not just
-- data cleanup — nothing is deleted (reconciled_duplicate_of preserves
-- which appointment each loser was superseded by), but whoever was
-- counting on the cancelled appointment needs to be told.
--
-- Review step — every slot with more than one confirmed appointment,
-- oldest first:
--
--   SELECT slot_id, id, patient_id, provider, scheduled_for, created_at
--   FROM appointments
--   WHERE status = 'confirmed'
--     AND slot_id IN (
--       SELECT slot_id FROM appointments WHERE status = 'confirmed'
--       GROUP BY slot_id HAVING count(*) > 1
--     )
--   ORDER BY slot_id, created_at ASC, id ASC;
--
-- Once reviewed and approved, this keeps the earliest-created confirmed
-- row per slot and reclassifies every OTHER confirmed row for that slot to
-- 'cancelled_duplicate', stamped with reconciled_duplicate_of pointing at
-- the row it lost to. Re-run migration 013 afterward — its preflight will
-- now pass.
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
