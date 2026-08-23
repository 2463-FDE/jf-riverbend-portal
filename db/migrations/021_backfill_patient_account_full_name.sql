-- 021_backfill_patient_account_full_name — one-time data fix.
--
-- Every patient account created before this cycle's gateway fix (app.py's
-- `_activate_invitation`) was created with `full_name = NULL`: the activation
-- path never read the patient's name from `patients`, it just left the column
-- unset. Every screen that shows a patient's own identity — /my-results, the
-- approved agent-summary display — reads `full_name` off the account, so
-- every existing patient account displays no name at all, not merely a
-- rendering gap.
--
-- This is a data backfill, not a schema change, but it lives in this
-- directory (and apply.sh) so it runs the same way every other migration
-- does. Idempotent and safe to re-run: the WHERE clause only ever touches a
-- row this exact condition still matches, so a second run updates zero rows.
--
-- Deliberately does NOT touch a NON-blank full_name. A staff account's
-- full_name is set at seed/creation time and means something different (a
-- display name chosen for that account, not necessarily "the linked
-- patient's name" — though for a patient-role account those happen to be the
-- same fact); overwriting anything already populated would risk clobbering a
-- value some other path intentionally set.
UPDATE users
   SET full_name = patients.name
  FROM patients
 WHERE users.patient_id = patients.id
   AND users.role = 'patient'
   AND (users.full_name IS NULL OR btrim(users.full_name) = '');
