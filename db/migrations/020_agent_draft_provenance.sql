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
-- REQUIRED LIFECYCLE. A draft moves through a fixed sequence of states, and
-- only that sequence — the "eight-stage" required path (libs/agent_provenance)
-- ends at display, but the row itself keeps moving after that:
--
--   draft --> validated --> approved --> superseded
--          \            \
--           --> refused   --> rejected
--
-- draft, validated and refused carry no decider. approved and rejected each
-- name exactly one decider and exactly one decision timestamp of the matching
-- kind. superseded is reached ONLY from approved (a regeneration produces a
-- new version and retires the old one), and it RETAINS the original approval's
-- decider/timestamp rather than clearing it — the row is being superseded, not
-- un-approved. refused, rejected and superseded are terminal: none of them can
-- move again. The BEFORE UPDATE trigger below enforces this transition graph
-- directly; the CHECK constraints enforce the decision/validation state that
-- must hold at each status, so the two mechanisms cannot drift apart.
--
-- IMMUTABLE IDENTITY AND EVIDENCE. patient_id, version, generated_text,
-- provenance_label, correlation_id, model_id and prompt_version are frozen the
-- instant a row is inserted — none of them may ever change, under any status
-- transition. validation_code is the one exception that starts mutable: it is
-- NULL at 'draft' and is set exactly once, when the draft leaves 'draft'
-- status, then frozen for the row's remaining lifetime. Both rules are
-- enforced by the trigger, not left to convention.
--
-- PROVENANCE TRUTHFULNESS. The client requires the real/fixture/fallback label
-- to be trustworthy, not just present: a 'fallback' row cannot claim a model or
-- prompt version (no model ran), and a 'real'/'fixture' row cannot omit them
-- (something must name what produced the text). Enforced by a CHECK, not by
-- convention at the call site.
--
-- REAPPLICATION. This file is re-run by db/migrations/apply.sh against every
-- database at every deploy, including ones that already ran an EARLIER version
-- of this same migration during review. `CREATE TABLE IF NOT EXISTS` cannot
-- upgrade an existing table's constraints, so every constraint below that can
-- change is added via guarded `DROP CONSTRAINT IF EXISTS` + `ADD CONSTRAINT`
-- (never left inline in the CREATE TABLE), and both trigger functions are
-- `CREATE OR REPLACE` behind a `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER` —
-- so a database that ran the looser, earlier shape of this file is upgraded to
-- the current one instead of silently keeping the old, weaker rules.

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
    -- never be presented as model output. See agent_draft_provenance_truthful
    -- below for what each label requires of model_id/prompt_version.
    provenance_label TEXT NOT NULL
                     CHECK (provenance_label IN ('real', 'fixture', 'fallback')),

    -- Ties every stage of one request together: retrieval, provider call,
    -- validation, review, display. Not a secret and not patient data.
    correlation_id  TEXT NOT NULL,

    -- Model identity is configuration, not output. Recorded so a demo can say
    -- which model produced a draft; NULL for fallback, required for real/fixture
    -- (agent_draft_provenance_truthful).
    model_id        TEXT,

    -- Machine-readable outcome of deterministic validation. A reason code, not
    -- a message: a validator message could quote the text it rejected. NULL
    -- while status='draft'; 'PASS' for every post-validation non-refused state;
    -- a specific non-PASS code while 'refused' (agent_draft_validation_code_consistent).
    validation_code TEXT,

    -- THE CLINICAL ARTIFACT. Immutable once written — see the trigger below.
    -- This is what the clinician reviews and what the patient is shown; it is
    -- never regenerated at display time.
    generated_text  TEXT NOT NULL,

    -- Which prompt produced it. A version identifier, NOT the prompt itself:
    -- the prompt text stays out of the database exactly as it stays out of
    -- traces. NULL for fallback, required for real/fixture, same as model_id.
    prompt_version  TEXT,

    -- Reviewer / editor, by user id. An id is a reference; a username is an
    -- identifier, and identifiers do not belong in metadata columns.
    reviewed_by     INTEGER REFERENCES users(id),
    approved_at     TIMESTAMPTZ,
    rejected_at     TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (patient_id, version)
);

CREATE INDEX IF NOT EXISTS agent_draft_provenance_patient_idx
    ON agent_draft_provenance (patient_id);
CREATE INDEX IF NOT EXISTS agent_draft_provenance_correlation_idx
    ON agent_draft_provenance (correlation_id);

-- Exact per-status decision-completeness. Mirrors migration 018's
-- default-deny pattern, but 018 only has two decided states; this table has
-- four (approved/rejected/superseded, plus draft/validated/refused carrying
-- none), so "decided XOR undecided" is not precise enough — each status names
-- EXACTLY the columns that must and must not be set. Rewritten (was a looser
-- decided/undecided split) to close the gap: the old CHECK allowed 'approved'
-- with rejected_at set and approved_at NULL, which is not a coherent approval.
ALTER TABLE agent_draft_provenance
    DROP CONSTRAINT IF EXISTS agent_draft_decision_complete;
ALTER TABLE agent_draft_provenance
    ADD CONSTRAINT agent_draft_decision_complete CHECK (
        (status IN ('draft', 'validated', 'refused')
            AND reviewed_by IS NULL AND approved_at IS NULL AND rejected_at IS NULL)
        OR (status = 'approved'
            AND reviewed_by IS NOT NULL AND approved_at IS NOT NULL AND rejected_at IS NULL)
        OR (status = 'rejected'
            AND reviewed_by IS NOT NULL AND rejected_at IS NOT NULL AND approved_at IS NULL)
        OR (status = 'superseded'
            -- Retains the original approval's decider/timestamp — a supersede
            -- is not an un-approval, it is a newer version taking over.
            AND reviewed_by IS NOT NULL AND approved_at IS NOT NULL AND rejected_at IS NULL)
    );

-- Provenance truthfulness: a label the caller could set inconsistently with
-- model_id/prompt_version is not actually trustworthy. NULL-safe: every
-- nullable column tested is guarded with an explicit IS [NOT] NULL before any
-- comparison, because a bare equality against a NULL evaluates to NULL, and a
-- CHECK's NULL result is NOT a violation in Postgres — an unguarded
-- `model_id = 'x'` would silently let a NULL model_id through.
ALTER TABLE agent_draft_provenance
    DROP CONSTRAINT IF EXISTS agent_draft_provenance_truthful;
ALTER TABLE agent_draft_provenance
    ADD CONSTRAINT agent_draft_provenance_truthful CHECK (
        (provenance_label = 'fallback'
            AND model_id IS NULL AND prompt_version IS NULL)
        OR (provenance_label IN ('real', 'fixture')
            AND model_id IS NOT NULL AND btrim(model_id) <> ''
            AND prompt_version IS NOT NULL AND btrim(prompt_version) <> '')
    );

-- correlation_id and generated_text are NOT NULL at the column level already;
-- this closes the "empty string" loophole NOT NULL does not catch.
ALTER TABLE agent_draft_provenance
    DROP CONSTRAINT IF EXISTS agent_draft_correlation_id_nonblank;
ALTER TABLE agent_draft_provenance
    ADD CONSTRAINT agent_draft_correlation_id_nonblank
    CHECK (btrim(correlation_id) <> '');

ALTER TABLE agent_draft_provenance
    DROP CONSTRAINT IF EXISTS agent_draft_generated_text_nonblank;
ALTER TABLE agent_draft_provenance
    ADD CONSTRAINT agent_draft_generated_text_nonblank
    CHECK (btrim(generated_text) <> '');

ALTER TABLE agent_draft_provenance
    DROP CONSTRAINT IF EXISTS agent_draft_version_positive;
ALTER TABLE agent_draft_provenance
    ADD CONSTRAINT agent_draft_version_positive CHECK (version > 0);

-- Validation-code consistency: 'draft' has none yet; every post-validation
-- non-refused status carries the machine-readable pass code; 'refused' carries
-- its own non-PASS refusal code. Same NULL-safety discipline as
-- agent_draft_provenance_truthful above — `validation_code = 'PASS'` alone
-- would pass a NULL validation_code through, so it is guarded with
-- `validation_code IS NOT NULL AND` first.
ALTER TABLE agent_draft_provenance
    DROP CONSTRAINT IF EXISTS agent_draft_validation_code_consistent;
ALTER TABLE agent_draft_provenance
    ADD CONSTRAINT agent_draft_validation_code_consistent CHECK (
        (status = 'draft' AND validation_code IS NULL)
        OR (status IN ('validated', 'approved', 'rejected', 'superseded')
            AND validation_code IS NOT NULL AND validation_code = 'PASS')
        OR (status = 'refused'
            AND validation_code IS NOT NULL AND btrim(validation_code) <> ''
            AND validation_code <> 'PASS')
    );

-- At most one PATIENT-VISIBLE version at a time. Concurrency-safe by
-- construction (a unique index, not an application-level check-then-insert):
-- two concurrent approvals for the same patient cannot both commit, even under
-- serializable anomalies, because the second INSERT/UPDATE into 'approved'
-- violates the index before either transaction can observe the other's
-- uncommitted row. A regeneration must move the old approved row to
-- 'superseded' in the SAME transaction that approves the new version, or the
-- new approval is rejected by this index — which is the intended ordering: the
-- old version stops being visible at the same moment the new one starts being
-- eligible to be, never both, never neither.
DROP INDEX IF EXISTS agent_draft_one_approved_per_patient;
CREATE UNIQUE INDEX agent_draft_one_approved_per_patient
    ON agent_draft_provenance (patient_id)
    WHERE status = 'approved';

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

-- A citation that names an empty source/citation id is not evidence of
-- anything and would defeat the "cited ids are a subset of retrieved ids"
-- check in libs/patient_view_agent/composer.py by comparing against blanks.
ALTER TABLE agent_draft_citation
    DROP CONSTRAINT IF EXISTS agent_draft_citation_fields_nonblank;
ALTER TABLE agent_draft_citation
    ADD CONSTRAINT agent_draft_citation_fields_nonblank CHECK (
        btrim(source_id) <> '' AND btrim(source_version) <> '' AND btrim(citation_id) <> ''
    );

-- IDENTITY, EVIDENCE AND LIFECYCLE GUARD for agent_draft_provenance.
--
-- Four separate guarantees, none of which a CHECK constraint alone can make
-- (a CHECK only sees one row's final state; these compare OLD to NEW, or need
-- to run on DELETE, which no CHECK ever sees):
--
--  1. patient_id, version, generated_text, provenance_label, correlation_id,
--     model_id and prompt_version never change after insert. Without this,
--     "the patient sees exactly the version the clinician approved" is a
--     convention one UPDATE silently breaks, and nothing downstream would
--     notice — the row would still look approved.
--  2. validation_code is set exactly once, when the row leaves 'draft'. A
--     re-validated draft is a new version, not a rewritten code on the old one.
--  3. status only advances along the required transition graph documented at
--     the top of this file. Every other transition is rejected, including any
--     move out of a terminal state (refused/rejected/superseded).
--  4. a row that has left 'draft' can never be DELETEd, full stop — direct,
--     deterministic, and independent of whether it has any citations. (An
--     earlier version of this migration relied on the citation cascade to
--     block this indirectly, which meant a decided draft with ZERO citations
--     had no protection at all — a real gap, not a documented tradeoff. This
--     explicit guard closes it.) A 'draft' row may still be deleted (e.g. an
--     unwanted generation, discarded before validation); its citations
--     cascade-delete cleanly — see the citation guard's DELETE branch below
--     for how it distinguishes that cascade from a standalone attempt.
--
-- Tamper-evidence beyond outright deletion refusal (e.g. a hash chain over
-- history) is explicitly NOT built here — that is w8-planner-2 B3 (audit
-- integrity). This guard only guarantees a decided draft cannot be removed;
-- it does not yet make removal attempts themselves auditable.
--
-- Supersedes the earlier, narrower `agent_draft_text_is_immutable` trigger,
-- which only froze text/version/patient_id, fired on UPDATE only, and
-- enforced no transition rule at all — a caller could previously move
-- 'refused' -> 'approved' directly, rewrite validation_code on an
-- already-validated row, or delete a decided draft that happened to have no
-- citations.
CREATE OR REPLACE FUNCTION agent_draft_provenance_guard()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION
                'agent_draft_provenance rows are never deleted once they leave '
                '''draft'' status (draft id=%, status=%). Deletion policy beyond '
                'this refusal (e.g. a hash-chained audit trail) is w8-planner-2 B3; '
                'until then, a decided draft is simply never removable, regardless '
                'of whether it has citations.', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;

    -- TG_OP = 'UPDATE' from here on.
    IF NEW.patient_id IS DISTINCT FROM OLD.patient_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.generated_text IS DISTINCT FROM OLD.generated_text
       OR NEW.provenance_label IS DISTINCT FROM OLD.provenance_label
       OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id
       OR NEW.model_id IS DISTINCT FROM OLD.model_id
       OR NEW.prompt_version IS DISTINCT FROM OLD.prompt_version
    THEN
        RAISE EXCEPTION
            'agent_draft_provenance identity/evidence is immutable once written '
            '(draft id=%, version=%): patient_id, version, generated_text, '
            'provenance_label, correlation_id, model_id and prompt_version never '
            'change after insert. A corrected or regenerated draft is a NEW version.',
            OLD.id, OLD.version;
    END IF;

    IF NEW.validation_code IS DISTINCT FROM OLD.validation_code
       AND OLD.status <> 'draft'
    THEN
        RAISE EXCEPTION
            'agent_draft_provenance.validation_code may only be set once, when '
            'leaving draft status (draft id=%, current status=%)', OLD.id, OLD.status;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'draft'        AND NEW.status IN ('validated', 'refused'))
            OR (OLD.status = 'validated' AND NEW.status IN ('approved', 'rejected'))
            OR (OLD.status = 'approved'  AND NEW.status = 'superseded')
        ) THEN
            RAISE EXCEPTION
                'invalid agent_draft_provenance status transition % -> % (draft id=%). '
                'Allowed: draft->validated|refused, validated->approved|rejected, '
                'approved->superseded. refused/rejected/superseded are terminal.',
                OLD.status, NEW.status, OLD.id;
        END IF;
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_draft_immutable_text ON agent_draft_provenance;
DROP TRIGGER IF EXISTS agent_draft_provenance_guard_trigger ON agent_draft_provenance;
CREATE TRIGGER agent_draft_provenance_guard_trigger
    BEFORE UPDATE OR DELETE ON agent_draft_provenance
    FOR EACH ROW EXECUTE FUNCTION agent_draft_provenance_guard();

-- Drop the function the trigger above supersedes. An orphaned function would
-- silently invite someone to re-attach it later, reintroducing the exact
-- transition/freeze/deletion gaps this migration closes.
DROP FUNCTION IF EXISTS agent_draft_text_is_immutable();

-- CITATION IMMUTABILITY GUARD.
--
--  * UPDATE is forbidden outright — a citation is a fact about what a specific
--    draft version cited; there is no correction, only a new draft.
--  * INSERT is allowed only while the parent draft is still 'draft' — once a
--    draft has been validated, the set of things it cited is exactly what was
--    validated, and adding one after the fact would let a reviewer approve
--    evidence the validator never saw.
--  * DELETE is allowed only while the parent draft is still 'draft' (same
--    reason in reverse), OR when the parent row is no longer found at all —
--    which, given the parent's own BEFORE DELETE guard above only ever
--    permits deleting a 'draft' parent, can only mean this DELETE is running
--    as part of the FK's ON DELETE CASCADE from that already-validated,
--    already-permitted parent deletion (Postgres deletes the parent tuple
--    before the cascade fires the child's trigger, within the same
--    transaction, so this trigger's own lookup legitimately finds no row).
--    Treating "parent not found" as forbidden here — the earlier version of
--    this file did — made it impossible to ever delete a draft parent that
--    had any citations at all, since its own legitimate cascade would trip
--    this exact guard.
--
-- CONCURRENCY. Both the INSERT and DELETE lookups below take `FOR SHARE` on
-- the parent row, not a plain SELECT. Without it, this citation write and a
-- concurrent `UPDATE agent_draft_provenance SET status = ...` on the same
-- parent race: a plain SELECT does not block on, or see, an in-flight
-- concurrent UPDATE, so a citation insert could commit having read 'draft'
-- from a snapshot that a concurrent validation was, at that same moment,
-- already committing past. `FOR SHARE` forces whichever of the two runs
-- second to wait for the first to commit and then see its result — so a
-- citation write and a status transition on the same draft are always
-- serialized against each other, in whichever order they actually occur.
-- (The transition guard's own UPDATE needs no equivalent fix: an UPDATE
-- statement always takes its own row lock as part of executing, so it is
-- already serialized against any concurrent writer of that same row.)
CREATE OR REPLACE FUNCTION agent_draft_citation_guard()
RETURNS TRIGGER AS $$
DECLARE
    parent_status TEXT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'agent_draft_citation rows are immutable — no field may be changed '
            'once written (citation id=%). A corrected citation set belongs to a '
            'NEW draft version.', OLD.id;
    ELSIF TG_OP = 'INSERT' THEN
        SELECT status INTO parent_status
            FROM agent_draft_provenance WHERE id = NEW.draft_id
            FOR SHARE;
        IF parent_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION
                'a citation may only be added while its draft is still in '
                '''draft'' status (draft id=%, status=%)',
                NEW.draft_id, COALESCE(parent_status, '<no such draft>');
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status
            FROM agent_draft_provenance WHERE id = OLD.draft_id
            FOR SHARE;
        -- parent_status IS NULL means no row was found: legitimate only as
        -- part of a cascade from a permitted 'draft'-parent deletion (see
        -- above) — allow it. A FOUND row that is not 'draft' is a standalone
        -- attempt to remove evidence from a decided draft — forbid it.
        IF parent_status IS NOT NULL AND parent_status <> 'draft' THEN
            RAISE EXCEPTION
                'a citation may not be removed once its draft has left ''draft'' '
                'status (citation id=%, draft status=%)',
                OLD.id, parent_status;
        END IF;
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_draft_citation_guard_trigger ON agent_draft_citation;
CREATE TRIGGER agent_draft_citation_guard_trigger
    BEFORE INSERT OR UPDATE OR DELETE ON agent_draft_citation
    FOR EACH ROW EXECUTE FUNCTION agent_draft_citation_guard();
