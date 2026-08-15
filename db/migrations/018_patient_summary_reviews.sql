-- 018_patient_summary_reviews — the clinician gate (S3).
--
-- The client's requirement is that the review queue is not decoration: the
-- approve/reject decision must control what the patient can actually see.
-- That makes this table an authorization input, not a workflow log, and it is
-- designed accordingly.
--
-- DEFAULT DENY. Patient visibility is granted only by an explicit `approved`
-- row. No row means not visible; a `pending` row means not visible; a
-- `rejected` row means not visible. There is deliberately no state that means
-- "visible unless someone objects" — a queue that fails open would show a
-- patient exactly the content the refusal path exists to withhold.
--
-- What queues: a result the deterministic renderer could not quote cleanly
-- (services/records-service/patient_summary.py). Directly-supported facts —
-- quoted values, dates, verbatim ranges, single-value arithmetic — reach the
-- patient without a clinician, because none of those is a clinical judgment.
-- That split is the client's, not ours.

CREATE TABLE IF NOT EXISTS patient_summary_reviews (
    id          SERIAL PRIMARY KEY,

    -- Denormalised from records.patient_id on purpose: every read of this
    -- table is "what is pending for this patient" or "may this patient see
    -- this record", and both must be answerable without joining `records`.
    -- The trigger-free guarantee that these agree is that only the enqueue
    -- path writes them, from the same row.
    patient_id  INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    record_id   INTEGER NOT NULL REFERENCES records(id)  ON DELETE CASCADE,

    -- pending | approved | rejected. Constrained rather than free text: this
    -- column gates chart content, and a typo that fell through to a default
    -- would be a silent disclosure.
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'approved', 'rejected')),

    -- Why it queued, in the renderer's own words. Not shown to the patient.
    reason      TEXT,

    decided_by  INTEGER REFERENCES users(id),
    decided_at  TIMESTAMPTZ,
    -- The clinician's note on the decision. Never surfaced to the patient:
    -- releasing a record is a decision about the report's own words, not an
    -- opportunity to add unreviewed commentary to a chart.
    decision_note TEXT,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A decision must carry its decider and its time; a pending row must carry
-- neither. Without this, a row could read `approved` with no accountable
-- clinician behind it — which is precisely the accountability the queue
-- exists to create.
ALTER TABLE patient_summary_reviews
    DROP CONSTRAINT IF EXISTS patient_summary_reviews_decision_complete;
ALTER TABLE patient_summary_reviews
    ADD CONSTRAINT patient_summary_reviews_decision_complete CHECK (
        (state = 'pending'  AND decided_by IS NULL AND decided_at IS NULL)
        OR
        (state <> 'pending' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
    );

-- At most one live review per record. Two pending rows for one record would
-- let two clinicians decide the same content differently, and the reader
-- would have no defensible way to choose between them.
CREATE UNIQUE INDEX IF NOT EXISTS patient_summary_reviews_one_pending_per_record
    ON patient_summary_reviews (record_id)
    WHERE state = 'pending';

-- The read path asks "is there an approved review for these record ids".
CREATE INDEX IF NOT EXISTS patient_summary_reviews_record_state_idx
    ON patient_summary_reviews (record_id, state);

-- The queue screen asks "what is pending", newest first.
CREATE INDEX IF NOT EXISTS patient_summary_reviews_pending_idx
    ON patient_summary_reviews (state, created_at DESC);

CREATE INDEX IF NOT EXISTS patient_summary_reviews_patient_idx
    ON patient_summary_reviews (patient_id);
