-- 020_agent_draft_provenance — the September 2 agentic demo's provenance spine.
--
-- WHAT THIS TABLE HOLDS, AND WHAT IT DELIBERATELY DOES NOT
--
-- The client's constraint is explicit: persist only provenance metadata —
-- source id/version, citation ids, categories, status, timestamps, correlation
-- id. Never persist prompts, model output, retrieved text, patient data,
-- identifiers, credentials, or raw provider errors.
--
-- So there is NO content column here, and that is not an omission. Every column
-- below is metadata about a generation, not the generation.
--
-- DECIDED 2026-08-21 (Option A). The generated draft text IS persisted here,
-- as a dedicated clinical artifact. See adr/0010 for the full boundary.
--
-- The reasoning, because it is not obvious: `patient_summary_reviews` (018)
-- stores no text at all — approval points at a record_id and the DETERMINISTIC
-- renderer regenerates the summary at display time. That is safe precisely
-- because it is deterministic. A model response is NOT reproducible, so
-- regenerating at display could show the patient something the clinician never
-- approved. A hash-only design fails the same way from the other direction: the
-- regenerated text would differ almost every time, so display would refuse
-- almost always.
--
-- So the prohibition on persisting model output is scoped to LOGS, TRACES,
-- TELEMETRY, PROMPTS AND OBSERVABILITY — not to the clinical artifact that must
-- be reviewed and released. `libs/agent_provenance` enforces the telemetry half
-- by raising on any attempt to put draft text into a trace.
--
-- ⚠️ `generated_text` IS PHI. It must be included in the encryption-at-rest
-- work (w8-planner-2 B2) and must never be copied into a trace, a log, an
-- analytics path, or a provider prompt without going through libs/deid.
--
-- Additive and guarded, so it is safe to re-apply at any prior migration point
-- (see apply.sh).

CREATE TABLE IF NOT EXISTS agent_draft_provenance (
    id              SERIAL PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,

    -- Monotonic per patient. Version 2 never mutates version 1: a regenerated
    -- draft is a new row, so "which version did the clinician approve" stays
    -- answerable after a regeneration.
    version         INTEGER NOT NULL,

    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'validated', 'refused',
                                      'approved', 'rejected', 'superseded')),

    -- real   = a live provider call produced this
    -- fixture= a recorded response was replayed
    -- fallback = the deterministic path ran; no model was called
    -- The client requires these labels to be explicit. `fallback` text must
    -- never be presented as model output.
    provenance_label TEXT NOT NULL
                     CHECK (provenance_label IN ('real', 'fixture', 'fallback')),

    -- Ties every stage of one request together: retrieval, provider call,
    -- validation, review, display. Not a secret and not patient data.
    correlation_id  TEXT NOT NULL,

    -- Model identity is configuration, not output. Recorded so a demo can say
    -- which model produced a draft; NULL for fallback.
    model_id        TEXT,

    -- Machine-readable outcome of deterministic validation. A reason code, not
    -- a message: a validator message could quote the text it rejected.
    validation_code TEXT,

    -- THE CLINICAL ARTIFACT. Immutable once written — see the trigger below.
    -- This is what the clinician reviews and what the patient is shown; it is
    -- never regenerated at display time.
    generated_text  TEXT NOT NULL,

    -- Which prompt produced it. A version identifier, NOT the prompt itself:
    -- the prompt text stays out of the database exactly as it stays out of
    -- traces.
    prompt_version  TEXT,

    -- Reviewer / editor, by user id. An id is a reference; a username is an
    -- identifier, and identifiers do not belong in metadata columns.
    reviewed_by     INTEGER REFERENCES users(id),
    approved_at     TIMESTAMPTZ,
    rejected_at     TIMESTAMPTZ,

    -- A decided draft must name its decider and its moment, and an undecided
    -- one must claim neither. Mirrors migration 018's constraint deliberately:
    -- the review gate's default-deny semantics are the pattern to copy, not to
    -- reinvent.
    CONSTRAINT agent_draft_decision_complete CHECK (
        (status IN ('approved', 'rejected')
            AND reviewed_by IS NOT NULL
            AND (approved_at IS NOT NULL OR rejected_at IS NOT NULL))
        OR
        (status NOT IN ('approved', 'rejected')
            AND approved_at IS NULL AND rejected_at IS NULL)
    ),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (patient_id, version)
);

CREATE INDEX IF NOT EXISTS agent_draft_provenance_patient_idx
    ON agent_draft_provenance (patient_id);
CREATE INDEX IF NOT EXISTS agent_draft_provenance_correlation_idx
    ON agent_draft_provenance (correlation_id);

-- One row per citation a draft made. Source id AND version, because an approved
-- document can be superseded and an approval must remain interpretable against
-- the version that was actually cited.
CREATE TABLE IF NOT EXISTS agent_draft_citation (
    id              SERIAL PRIMARY KEY,
    draft_id        INTEGER NOT NULL REFERENCES agent_draft_provenance(id) ON DELETE CASCADE,
    source_id       TEXT NOT NULL,
    source_version  TEXT NOT NULL,
    citation_id     TEXT NOT NULL,
    category        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (draft_id, citation_id)
);

CREATE INDEX IF NOT EXISTS agent_draft_citation_draft_idx
    ON agent_draft_citation (draft_id);
CREATE INDEX IF NOT EXISTS agent_draft_citation_source_idx
    ON agent_draft_citation (source_id, source_version);

-- IMMUTABILITY. The status of a draft moves (draft -> validated -> approved),
-- but its TEXT and its version never do. Without this, "the patient sees
-- exactly the version the clinician approved" is a convention that one UPDATE
-- silently breaks — and nothing downstream would notice, because the row would
-- still look approved.
CREATE OR REPLACE FUNCTION agent_draft_text_is_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.generated_text IS DISTINCT FROM OLD.generated_text THEN
        RAISE EXCEPTION
            'agent_draft_provenance.generated_text is immutable (draft id=%, version=%). '
            'A revised summary is a NEW version, not an edit of an approved one.',
            OLD.id, OLD.version;
    END IF;
    IF NEW.version IS DISTINCT FROM OLD.version
       OR NEW.patient_id IS DISTINCT FROM OLD.patient_id THEN
        RAISE EXCEPTION 'agent_draft_provenance identity is immutable (draft id=%)', OLD.id;
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_draft_immutable_text ON agent_draft_provenance;
CREATE TRIGGER agent_draft_immutable_text
    BEFORE UPDATE ON agent_draft_provenance
    FOR EACH ROW EXECUTE FUNCTION agent_draft_text_is_immutable();
