-- demo_reset.sql — return the four canonical demo patients to a clean,
-- rehearsable state: 1042 (Maria Gonzalez, hyperlipidemia + duplicate-record
-- reconciliation), 1737 (Priya Khan, diabetes + invitation/activation), 1738
-- (Thomas Johnson, hypertension, pre-activated), 1739 (Aisha Taylor, asthma,
-- pre-activated).
--
-- WHY THIS EXISTS
-- The clinician review gate is deliberately durable: a rejected record is
-- never re-queued, and an approved one stays released. That is the property
-- the client asked for, and it means every rehearsal CONSUMES demo state —
-- after one full run a patient has an approval, a rejection and one
-- remaining case; after two, the queue is empty and the clinician beat cannot
-- be shown at all. The integration suite consumes it the same way.
--
-- So this is not a fixture repair — it is the counterpart to a feature working
-- correctly. Run it before every rehearsal and after every test run.
--
-- TWO DIFFERENT "clean states", by design (2026-08-22)
-- 1042 and 1737 are demonstrated INVITE-READY: their portal account, grant
-- and any invitation are deleted, so the demo can start from "front desk
-- issues a code." 1738 and 1739 are demonstrated PRE-ACTIVATED: their portal
-- account is a fixed, documented credential (see db/seed/generate_seed.py's
-- PATIENT_DEMO_PASSWORD) that the reset RESTORES to active rather than
-- deleting — a test or rehearsal that revoked the grant or deactivated the
-- account must not require a re-seed to fix, the same reasoning that already
-- applied to drkim's reviewer grant below.
--
-- WHAT IT TOUCHES
-- Only these four patients' portal/review state — never 1330/1588 (the
-- intentionally incomplete duplicate-chart candidates), never any other
-- patient. It does NOT delete records, encounters, patients, or agent draft
-- history: the chart, the trends and the clinician accounts all come from
-- db/seed/seed.sql and are left alone. `agent_draft_provenance` rows are
-- IMMUTABLE once validated (migration 020's guard) and are never touched here
-- regardless — see the note before the verification table for what that means
-- for a fully virgin agent-draft demonstration.
--
-- Safe to run repeatedly, and safe to run when nothing exists yet.

\set canonical_patients '(1042, 1737, 1738, 1739)'
\set demo_patient 1737

BEGIN;

-- Review decisions, all four patients. Removing these returns any refused
-- result to `pending` on the patient's next read, because the summary path
-- re-queues anything it refuses that has no review row at all.
DELETE FROM patient_summary_reviews WHERE patient_id IN :canonical_patients;

-- --- 1042 and 1737: INVITE-READY -------------------------------------------
-- Portal account, its access grant and any invitation, so the demo can start
-- from "front desk issues a code" rather than from an account that already
-- exists.
DELETE FROM patient_access_grants
 WHERE user_id IN (SELECT id FROM users WHERE patient_id IN (1042, 1737));
DELETE FROM patient_invitations WHERE patient_id IN (1042, 1737);
DELETE FROM users WHERE patient_id IN (1042, 1737);

-- --- 1738 and 1739: PRE-ACTIVATED -------------------------------------------
-- The account itself is restored to active rather than deleted — a rehearsal
-- has no invitation beat to replay for these two, so there is nothing to
-- reset it back TO except "still signed-in-able." Any leftover invitation
-- (e.g. from a test that issued one anyway) is still cleared: an outstanding
-- invitation for an already-active account is a contradiction, not a valid
-- state, whichever patient it is for.
DELETE FROM patient_invitations WHERE patient_id IN (1738, 1739);
UPDATE users SET is_active = TRUE
 WHERE patient_id IN (1738, 1739) AND role = 'patient' AND is_active = FALSE;

-- --- Staff/clinician grants, all four — restore ACTIVE, never merely EXISTS
--
-- "A row exists" is not the condition that matters. The queue and every
-- grant-scoped read are scoped by active_patient_ids_query
-- (services/records-service/patient_access_gate.py), which additionally
-- requires revoked_at IS NULL and a non-expired expires_at. A revoked or
-- expired row therefore suppresses a guarded-on-absence insert while leaving
-- the grant inert — and the demo operator sees a reset that reported success
-- and an empty queue/chart, with nothing on screen connecting the two.
--
-- So this clears revoked_at and expires_at on an existing row rather than
-- skipping it, and inserts only when there is genuinely nothing there — one
-- pass per (username, patient_id) pair, covering every staff/clinician grant
-- and both patient self-grants the canonical fixtures need:
--   frontdesk : 1042, 1737, 1738, 1739 (registers all four)
--   rdelgado  : 1042                   (duplicate-records demo, see generate_seed.py)
--   drkim     : 1737                   (S3 review queue)
--   drpatel   : 1738                   (hypertension)
--   drnguyen  : 1739                   (asthma)
--   patient-1738, patient-1739         (self-grants — the pre-activated accounts)
WITH pairs (username, patient_id) AS (
    VALUES
        ('frontdesk', 1042), ('frontdesk', 1737), ('frontdesk', 1738), ('frontdesk', 1739),
        ('rdelgado', 1042),
        ('drkim', 1737),
        ('drpatel', 1738),
        ('drnguyen', 1739),
        ('patient-1738', 1738),
        ('patient-1739', 1739)
)
UPDATE patient_access_grants g
   SET revoked_at = NULL,
       expires_at = NULL
  FROM users u, pairs p
 WHERE u.id = g.user_id
   AND u.username = p.username
   AND g.patient_id = p.patient_id
   AND (g.revoked_at IS NOT NULL OR g.expires_at IS NOT NULL);

WITH pairs (username, patient_id) AS (
    VALUES
        ('frontdesk', 1042), ('frontdesk', 1737), ('frontdesk', 1738), ('frontdesk', 1739),
        ('rdelgado', 1042),
        ('drkim', 1737),
        ('drpatel', 1738),
        ('drnguyen', 1739),
        ('patient-1738', 1738),
        ('patient-1739', 1739)
)
INSERT INTO patient_access_grants (user_id, patient_id)
SELECT u.id, p.patient_id
  FROM users u
  JOIN pairs p ON p.username = u.username
 WHERE NOT EXISTS (
        SELECT 1 FROM patient_access_grants g
         WHERE g.user_id = u.id AND g.patient_id = p.patient_id
       );

COMMIT;

-- What the operator should see, one row per canonical patient. Anything
-- surprising here — a portal_account that should exist and does not, a
-- reviewer_grant that is not 'active', a trend_results count under 2, an
-- encounters/appointments count of 0 — means the database predates the
-- current seed file. `make seed` will NOT fix that (it fails on a non-empty
-- database); re-seed with `docker compose down -v && make up`.
--
-- A REAL Bedrock call against 1737's chart also writes an agent_draft_
-- provenance row, and that row is IMMUTABLE once validated (migration 020) —
-- this script never deletes it, by design (see the note above). So a
-- completely VIRGIN agent-draft demonstration (no prior version at all, not
-- even a superseded one) is only possible starting from a FRESH volume:
--     docker compose down -v && make up
-- A reset alone is sufficient for every other beat, including a REPEAT
-- agent-draft demonstration — the versioning working (a new version pending
-- while the previous stays approved) is itself a valid, arguably better beat.
\echo ''
\echo '  after reset — one row per canonical patient:'
SELECT
    p.id AS patient_id,
    p.name,
    coalesce(u.username, 'none') AS portal_account,
    coalesce(ic.status, 'none') AS coverage,
    coalesce(enc.n, 0) AS encounters,
    coalesce(rec.n, 0) AS records,
    coalesce(trend.n, 0) AS trend_results,
    coalesce(appt.n, 0) AS appointments,
    coalesce(rev.n, 0) AS pending_reviews,
    CASE
        WHEN grant_check.reviewer_role IS NULL THEN 'NO STAFF GRANT — re-seed: docker compose down -v && make up'
        ELSE grant_check.reviewer_role
    END AS active_grants
FROM patients p
LEFT JOIN users u ON u.patient_id = p.id AND u.role = 'patient'
LEFT JOIN LATERAL (
    SELECT status FROM insurance_coverages WHERE patient_id = p.id ORDER BY id DESC LIMIT 1
) ic ON true
LEFT JOIN LATERAL (SELECT count(*) AS n FROM encounters WHERE patient_id = p.id) enc ON true
LEFT JOIN LATERAL (SELECT count(*) AS n FROM records WHERE patient_id = p.id) rec ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM records
     WHERE patient_id = p.id AND kind = 'lab_result'
       AND title IN ('A1c', 'LDL', 'Systolic BP', 'SpO2')
) trend ON true
LEFT JOIN LATERAL (SELECT count(*) AS n FROM appointments WHERE patient_id = p.id) appt ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM patient_summary_reviews WHERE patient_id = p.id AND state = 'pending'
) rev ON true
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT gu.username, ', ' ORDER BY gu.username) AS reviewer_role
      FROM patient_access_grants g
      JOIN users gu ON gu.id = g.user_id
     WHERE g.patient_id = p.id
       AND gu.is_active
       AND g.revoked_at IS NULL
       AND (g.expires_at IS NULL OR g.expires_at > now())
) grant_check ON true
WHERE p.id IN :canonical_patients
ORDER BY p.id;
