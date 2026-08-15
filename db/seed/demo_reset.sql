-- demo_reset.sql — return the demo patient to a clean pre-demo state.
--
-- WHY THIS EXISTS
-- The clinician review gate is deliberately durable: a rejected record is
-- never re-queued, and an approved one stays released. That is the property
-- the client asked for, and it means every rehearsal CONSUMES demo state.
-- After one full run patient 1737 has an approval, a rejection and one
-- remaining case; after two, the queue is empty and the clinician beat cannot
-- be shown at all. The integration suite consumes it the same way.
--
-- So this is not a fixture repair — it is the counterpart to a feature working
-- correctly. Run it before every rehearsal and after every test run.
--
-- WHAT IT TOUCHES
-- Only the demo patient's portal state. It does NOT delete records, encounters
-- or patients, and it does not re-seed: the chart, the A1c trend and the
-- clinician account all come from db/seed/seed.sql and are left alone.
--
-- Safe to run repeatedly, and safe to run when nothing exists yet.

\set demo_patient 1737

BEGIN;

-- Review decisions. Removing these returns the refused results to `pending`
-- on the patient's next read, because the summary path re-queues anything it
-- refuses that has no review row at all.
DELETE FROM patient_summary_reviews WHERE patient_id = :demo_patient;

-- The patient's own portal account, its access grant and any invitation, so
-- the demo can start from "front desk issues a code" rather than from an
-- account that already exists.
DELETE FROM patient_access_grants
 WHERE user_id IN (SELECT id FROM users WHERE patient_id = :demo_patient);
DELETE FROM patient_invitations WHERE patient_id = :demo_patient;
DELETE FROM users WHERE patient_id = :demo_patient;

-- The reviewing clinician's grant. Seeded (drkim, user 13), but re-asserted
-- here so a database seeded before that account existed still ends up in a
-- demonstrable state instead of an empty review queue with no explanation.
INSERT INTO patient_access_grants (user_id, patient_id)
SELECT u.id, :demo_patient
  FROM users u
 WHERE u.username = 'drkim'
   AND NOT EXISTS (
        SELECT 1 FROM patient_access_grants g
         WHERE g.user_id = u.id AND g.patient_id = :demo_patient
       );

COMMIT;

-- What the operator should see. Anything other than these values means the
-- database predates the current seed file. `make seed` will NOT fix that (it
-- fails on a non-empty database); re-seed with `docker compose down -v && make up`.
\echo ''
\echo '  after reset — expected:  reviews=0  portal_account=none  reviewer=clinician  a1c_results=2'
SELECT
    (SELECT count(*) FROM patient_summary_reviews WHERE patient_id = 1737) AS reviews,
    (SELECT coalesce(max(username), 'none') FROM users WHERE patient_id = 1737) AS portal_account,
    (SELECT coalesce(max(role), 'MISSING — re-seed: docker compose down -v && make up') FROM users WHERE username = 'drkim') AS reviewer,
    (SELECT count(*) FROM records
      WHERE patient_id = 1737 AND kind = 'lab_result' AND title = 'A1c') AS a1c_results;
