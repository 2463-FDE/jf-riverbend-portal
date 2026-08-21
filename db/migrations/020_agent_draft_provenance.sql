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
-- ⚠️ OPEN DESIGN QUESTION, deliberately not answered by this migration.
-- `patient_summary_reviews` (018) also stores no text: approval points at a
-- record_id and the DETERMINISTIC renderer regenerates the summary at display
-- time. That works precisely because it is deterministic. A model-generated
-- draft is not reproducible, so "display exactly the approved version" cannot
-- be satisfied by regeneration. Whether the draft text is persisted, and where,
-- is a decision for the user — see w8-planner. This migration is additive and
-- does not foreclose either answer.
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
