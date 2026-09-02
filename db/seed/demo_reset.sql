-- demo_reset.sql — return the four canonical demo patients to a clean,
-- rehearsable state: 1042 (Maria Gonzalez, hyperlipidemia + duplicate-record
-- reconciliation), 1737 (Priya Khan, diabetes + invitation/activation), 1738
-- (Thomas Johnson, hypertension, pre-activated, the deliberate TWO-CLINICIAN
-- overlap patient), 1739 (Aisha Taylor, asthma, pre-activated).
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
-- WHAT THIS DOES **NOT** DO: prepopulate the review queue. Patient
-- summary_reviews rows are created lazily, by `records-service`'s own read
-- path, the first time a patient (or an authorized clinician's read of that
-- patient's summary) actually triggers `review_queue.enqueue_refusals`.
-- Immediately after a reset, EVERY canonical patient's `pending_reviews`
-- count is 0 — that is correct, not a bug: the queue has nothing in it until
-- someone opens the deterministic results/summary path for that patient. See
-- the verification query at the bottom, and the demo script in
-- docs/runbook.md / .claude/skills/w8-planner/SKILL.md for exactly which
-- request populates which patient's cases.
--
-- TWO DIFFERENT "clean states", by design (2026-08-22)
-- 1042 and 1737 are demonstrated INVITE-READY: their portal account, grant
-- and any invitation are deleted, so the demo can start from "front desk
-- issues a code." 1738 and 1739 are demonstrated PRE-ACTIVATED: their portal
-- account is a fixed, documented credential (see db/seed/generate_seed.py's
-- PATIENT_DEMO_PASSWORD) that the reset RESTORES to active rather than
-- deleting — a test or rehearsal that revoked a grant or deactivated an
-- account must not require a re-seed to fix.
--
-- THE CLINICIAN MATRIX (2026-08-22) — two clinician accounts, not one, with a
-- deliberate overlap:
--     drkim    : 1042, 1737, 1738   (NOT 1739)
--     drnguyen : 1738, 1739         (NOT 1042, NOT 1737)
-- 1738 is the overlap: both clinicians hold an active grant for it, so a
-- shared-queue listing and "one reviewer's decision is not overwritable by
-- the other" are both demonstrable. Neither clinician's grant on the other
-- three canonical patients is ever added here.
--
-- WHAT ELSE IT TOUCHES
-- Only these four patients' portal/review state and the two clinicians' own
-- grants on them — never 1330/1588 (the intentionally incomplete
-- duplicate-chart candidates), never drpatel's separate treating-provider
-- grant on 1738, never any other patient. It does NOT delete records,
-- encounters, patients, or agent draft history: the chart, the trends and
-- the clinician accounts all come from db/seed/seed.sql and are left alone.
-- `agent_draft_provenance` rows are IMMUTABLE once validated (migration 020's
-- guard) and are never touched here regardless — see the note before the
-- verification table for what that means for a fully virgin agent-draft
-- demonstration.
--
-- Safe to run repeatedly, and safe to run when nothing exists yet.

-- W10 Final 2 Stage 3: without this, psql's default behavior on a failing
-- statement (including the fail-closed guard below) is to print the error,
-- roll back only the current transaction, and keep running every remaining
-- statement in the file anyway — each one erroring against the now-aborted
-- transaction, then the verification queries below running as fresh,
-- separate (successful) statements against STALE data, exiting 0 as if
-- nothing had gone wrong. Setting this turns any error into an immediate,
-- whole-script stop with a nonzero exit code — the actual "fail closed"
-- this file's guard depends on.
\set ON_ERROR_STOP on

\set canonical_patients '(1042, 1737, 1738, 1739)'

-- Dedicated demo-booking pool (w9-fixes P0 4.2 follow-up), ids 95001-95016 —
-- see db/seed/generate_seed.py's DEMO_SLOT_IDS. Kept separate from every
-- other slot in the database specifically so this file can freely reset
-- them without ever touching a real chart's historical appointment history:
-- the 88200-88319 pool and the curated 90001-90008 fixtures are seed-time-
-- only and are never modified here.
--
-- WHY THIS EXISTS: scheduling-service now requires start_at to be in the
-- future (and GET /slots excludes any slot a confirmed appointment already
-- occupies) — see services/scheduling-service/app.py::list_slots and
-- book.py::_lock_open_slot. seed.sql's own dates for this pool are fixed at
-- generation time and eventually fall into the past, so without this reset
-- "Schedule a visit" would show no availability at all after enough time
-- passes, or after a rehearsal/test consumes one of them.
\set demo_slot_lo 95001
\set demo_slot_hi 95016

BEGIN;

-- W10 Final 2 Stage 3 — fail closed if the fixtures this reset depends on
-- are not the exact shape the current seed establishes, rather than
-- silently completing a partial reset (e.g. updating 0 coverage rows, or
-- restoring a thread's read-state against the wrong patient) and reporting
-- success anyway. Mirrors the existing "predates the current seed, re-seed
-- with docker compose down -v && make up" guidance at the bottom of this
-- file, but stops the transaction instead of only printing a warning.
DO $$
DECLARE
    coverage_patients INTEGER;
    thread_1738_patient INTEGER;
    thread_1739_patient INTEGER;
BEGIN
    SELECT count(DISTINCT patient_id) INTO coverage_patients
      FROM insurance_coverages WHERE patient_id IN (1042, 1737, 1738, 1739);
    IF coverage_patients != 4 THEN
        RAISE EXCEPTION 'demo_reset: expected an insurance_coverages row for all four canonical patients (1042, 1737, 1738, 1739), found %  — database predates the current seed; re-seed with docker compose down -v && make up', coverage_patients;
    END IF;

    SELECT patient_id INTO thread_1738_patient FROM message_threads WHERE id = 1;
    SELECT patient_id INTO thread_1739_patient FROM message_threads WHERE id = 2;
    IF thread_1738_patient IS DISTINCT FROM 1738 OR thread_1739_patient IS DISTINCT FROM 1739 THEN
        RAISE EXCEPTION 'demo_reset: expected message_threads id 1 for patient 1738 and id 2 for patient 1739 (seed.sql''s W9.2 fixtures), found patient_id % and % — database predates the current seed; re-seed with docker compose down -v && make up', thread_1738_patient, thread_1739_patient;
    END IF;
END $$;

-- Review decisions, all four patients. Removing these returns any refused
-- result to `pending` on the patient's next read, because the summary path
-- re-queues anything it refuses that has no review row at all. This is a
-- CLEAR, never a prepopulate: nothing is inserted into
-- patient_summary_reviews anywhere in this file.
DELETE FROM patient_summary_reviews WHERE patient_id IN :canonical_patients;

-- --- Dedicated demo-booking pool: reopen and reposition into the future ----
-- Only ever touches appointments whose slot_id falls in this reserved
-- range — no historical/canonical-patient appointment is deleted or
-- modified by this block, regardless of which patient it happens to be for.
DELETE FROM appointments WHERE slot_id BETWEEN :demo_slot_lo AND :demo_slot_hi;

WITH repositioned AS (
    SELECT id,
           now() + interval '1 day'
                 + ((id - :demo_slot_lo) / 4) * interval '1 day'
                 + ((id - :demo_slot_lo) % 4) * interval '2 hours' AS new_start
      FROM slots
     WHERE id BETWEEN :demo_slot_lo AND :demo_slot_hi
)
UPDATE slots s
   SET start_at = r.new_start,
       end_at   = r.new_start + interval '30 minutes',
       status   = 'open'
  FROM repositioned r
 WHERE s.id = r.id;

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

-- --- Named demo staff accounts — restored active, never deleted -------------
-- A test or rehearsal that deactivated either clinician or the scoped ROI
-- clerk must not leave the next demo unable to authenticate after a reset.
UPDATE users SET is_active = TRUE
 WHERE username IN ('drkim', 'drnguyen', 'dwhite') AND is_active = FALSE;

-- --- Staff/clinician/self grants — restore ACTIVE, never merely EXISTS -----
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
-- pass per (username, patient_id) pair, covering every grant the four
-- canonical fixtures need, exactly matching db/seed/generate_seed.py's own
-- INSERT INTO patient_access_grants for these four patients:
--   frontdesk : 1042, 1737, 1738, 1739 (registers all four)
--   rdelgado  : 1042                   (duplicate-records demo)
--   drpatel   : 1738                   (treating provider — hypertension)
--   drkim     : 1042, 1737, 1738       (clinician reviewer — NOT 1739)
--   drnguyen  : 1738, 1739             (clinician reviewer — NOT 1042, NOT 1737)
--   patient-1738, patient-1739         (self-grants — the pre-activated accounts)
--   dwhite    : 1042 ONLY              (least-privilege roi_clerk demo identity)
WITH pairs (username, patient_id) AS (
    VALUES
        ('frontdesk', 1042), ('frontdesk', 1737), ('frontdesk', 1738), ('frontdesk', 1739),
        ('rdelgado', 1042),
        ('drpatel', 1738),
        ('drkim', 1042), ('drkim', 1737), ('drkim', 1738),
        ('drnguyen', 1738), ('drnguyen', 1739),
        ('patient-1738', 1738),
        ('patient-1739', 1739),
        -- Demo-readiness slice: dwhite's ONE scoped ROI grant. Listed here so
        -- a reset restores it the same way as every other fixture grant —
        -- clears an accidental revoke/expiry and reinserts if missing —
        -- rather than leaving it as one-time seed data nothing repairs.
        ('dwhite', 1042)
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
        ('drpatel', 1738),
        ('drkim', 1042), ('drkim', 1737), ('drkim', 1738),
        ('drnguyen', 1738), ('drnguyen', 1739),
        ('patient-1738', 1738),
        ('patient-1739', 1739),
        -- Demo-readiness slice: dwhite's ONE scoped ROI grant. Listed here so
        -- a reset restores it the same way as every other fixture grant —
        -- clears an accidental revoke/expiry and reinserts if missing —
        -- rather than leaving it as one-time seed data nothing repairs.
        ('dwhite', 1042)
)
INSERT INTO patient_access_grants (user_id, patient_id)
SELECT u.id, p.patient_id
  FROM users u
  JOIN pairs p ON p.username = u.username
 WHERE NOT EXISTS (
        SELECT 1 FROM patient_access_grants g
         WHERE g.user_id = u.id AND g.patient_id = p.patient_id
       );

-- --- Coverage/eligibility — restore to seed.sql's own curated baseline -----
-- docs/runbook.md used to warn that coverage "reflects whatever the last
-- real eligibility check set it to and is NOT reset by make demo-reset" —
-- true before this stage, and a real gap: the Coverage & Eligibility
-- workspace's three deliberately-distinct starting states (1737 active,
-- 1738 stale, 1739 unknown — see db/seed/generate_seed.py's own comment on
-- why those three, not a random draw) silently drifted after the first
-- "Request verification" call and stayed drifted for every rehearsal after.
-- Restores status/verified_at and clears any in-flight verification_job_id
-- (migration 023) — never touches payer_name/member_id/group_number/
-- plan_type, which the app never mutates itself.
UPDATE insurance_coverages
   SET status = v.status, verified_at = v.verified_at, verification_job_id = NULL
  FROM (VALUES
            (1042, 'active', TIMESTAMPTZ '2026-06-22 09:14:30'),
            (1737, 'active', TIMESTAMPTZ '2026-03-05 08:55:00'),
            (1738, 'stale',  TIMESTAMPTZ '2026-03-18 09:25:00'),
            (1739, 'unknown', NULL::TIMESTAMPTZ)
       ) AS v(patient_id, status, verified_at)
 WHERE insurance_coverages.patient_id = v.patient_id;

-- --- Messaging — restore seed.sql's two curated threads (W9.2) -------------
-- thread 1 (patient 1738): unread by both of its granted clinicians.
-- thread 2 (patient 1739): a patient-asks/clinician-replies exchange the
-- PATIENT has already read, drnguyen has not. Neither status carries any
-- delete route at the application layer (thread_messages has none by
-- design — see migration 022's own comment), so a rehearsal that sends a
-- follow-up, marks a thread read, or closes one leaves that behind for the
-- next rehearsal without this reset.
DELETE FROM thread_read_state WHERE thread_id IN (1, 2);
DELETE FROM thread_messages WHERE thread_id IN (1, 2) AND id NOT IN (1, 2, 3);
INSERT INTO thread_read_state (thread_id, user_id, last_read_message_id, updated_at)
SELECT 2, u.id, 3, TIMESTAMPTZ '2026-08-18 15:00:00'
  FROM users u WHERE u.patient_id = 1739 AND u.role = 'patient';
UPDATE message_threads SET status = 'open', updated_at = v.updated_at
  FROM (VALUES
            (1, TIMESTAMPTZ '2026-08-20 09:00:00'),
            (2, TIMESTAMPTZ '2026-08-18 14:32:00')
       ) AS v(id, updated_at)
 WHERE message_threads.id = v.id;

-- --- ROI — clear, never prepopulate (same discipline as review decisions) --
-- Unlike coverage/messaging, seed.sql has no curated per-canonical-patient
-- ROI fixture to restore TO (its 16 roi_requests rows are drawn from the
-- FULL random patient pool, W10 Final 2's own scope note on "documented
-- baseline" doesn't apply here) — so a demo-ready canonical patient's ROI
-- state is simply "no leftover pending request/authorization from a prior
-- rehearsal", the same CLEAR-not-prepopulate pattern this file already uses
-- for patient_summary_reviews above. A FULFILLED request/authorization is
-- never touched: disclosures.roi_request_id/authorization_id reference them
-- with no ON DELETE CASCADE (by design — 45 CFR 164.508 accounting must
-- survive), so attempting to delete one Postgres still protects would abort
-- this whole transaction rather than silently orphaning the disclosure log.
DELETE FROM roi_requests WHERE patient_id IN :canonical_patients AND status != 'fulfilled';
DELETE FROM roi_authorizations
 WHERE patient_id IN :canonical_patients
   AND id NOT IN (SELECT authorization_id FROM disclosures WHERE authorization_id IS NOT NULL);
DELETE FROM roi_disclosure_restrictions WHERE patient_id IN :canonical_patients;

COMMIT;

\echo ''
\echo '  demo-booking pool (95001-95016) after reset:'
SELECT count(*) AS available_demo_slots
  FROM slots
 WHERE id BETWEEN :demo_slot_lo AND :demo_slot_hi
   AND status = 'open'
   AND start_at > now();

-- What the operator should see, one row per canonical patient. Anything
-- surprising here — a portal_account that should exist and does not, an
-- active_reviewers list missing an expected name, a trend_results count
-- under 2, an encounters/appointments count of 0 — means the database
-- predates the current seed file. `make seed` will NOT fix that (it fails on
-- a non-empty database); re-seed with `docker compose down -v && make up`.
--
-- `pending_reviews` is expected to be 0 for every row immediately after a
-- reset — that is not a partial reset, it is the queue's own lazy-population
-- design (see the note at the top of this file). Open each patient's
-- deterministic results/summary path to populate it; the demo script names
-- exactly which request does that for which patient.
--
-- `coverage` is now restored to seed.sql's own curated per-patient baseline
-- by this reset (W10 Final 2 Stage 3) — it no longer merely reflects
-- whatever the last real eligibility check happened to leave behind.
-- `thread` and `pending_roi` are new the same stage: `thread` reads 'none'
-- for 1042/1737 (no seeded thread — the messaging demo path is
-- 1738/1739-only, see migration 022) or `open/1msgs`/`open/2msgs` for 1738
-- and 1739 respectively immediately after a reset; `pending_roi` should
-- always be 0.
--
-- A REAL Bedrock call against any of these charts also writes an
-- agent_draft_provenance row, and that row is IMMUTABLE once validated
-- (migration 020) — this script never deletes it, by design (see the note
-- above). So a completely VIRGIN agent-draft demonstration (no prior version
-- at all, not even a superseded one) is only possible starting from a FRESH
-- volume:
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
    coalesce(reviewers.names, 'NONE — re-seed: docker compose down -v && make up') AS active_reviewers,
    coalesce(other_grants.names, 'none') AS other_active_grants,
    coalesce(thread.summary, 'none') AS thread,
    coalesce(roi.n, 0) AS pending_roi
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
-- Active reviewers: accounts on the 'clinician' (or 'nursing_ma') role — the
-- only roles holding summary_review.decide — with an active grant. Listed
-- separately from other_active_grants so "who can actually decide a case for
-- this patient" reads at a glance instead of being buried in a mixed list
-- that also contains front-desk registration and treating-provider grants.
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT gu.username, ', ' ORDER BY gu.username) AS names
      FROM patient_access_grants g
      JOIN users gu ON gu.id = g.user_id
     WHERE g.patient_id = p.id
       AND gu.is_active
       AND gu.role IN ('clinician', 'nursing_ma')
       AND g.revoked_at IS NULL
       AND (g.expires_at IS NULL OR g.expires_at > now())
) reviewers ON true
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT gu.username, ', ' ORDER BY gu.username) AS names
      FROM patient_access_grants g
      JOIN users gu ON gu.id = g.user_id
     WHERE g.patient_id = p.id
       AND gu.is_active
       AND gu.role NOT IN ('clinician', 'nursing_ma')
       AND g.revoked_at IS NULL
       AND (g.expires_at IS NULL OR g.expires_at > now())
) other_grants ON true
LEFT JOIN LATERAL (
    SELECT mt.status || '/' || count(tm.id) || 'msgs' AS summary
      FROM message_threads mt
      LEFT JOIN thread_messages tm ON tm.thread_id = mt.id
     WHERE mt.patient_id = p.id
     GROUP BY mt.id, mt.status
) thread ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS n FROM roi_requests WHERE patient_id = p.id AND status = 'pending'
) roi ON true
WHERE p.id IN :canonical_patients
ORDER BY p.id;
