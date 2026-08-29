-- 036_agent_lifecycle_events — W10 Final Stage 4.
--
-- Summary generation, clinician review, and approved display each
-- instantiated their OWN in-memory TraceRecorder (libs/agent_provenance),
-- scoped to a single HTTP request, and discarded it when that request
-- ended — three separate, temporally disjoint objects, none of them ever
-- persisted anywhere. "One trace across the eight stages" (the client's own
-- requirement, see that library's module docstring) was true only within a
-- single request; nothing let anyone reconstruct the WHOLE lifecycle of one
-- draft after the fact.
--
-- This table is the durable, append-only sink every route now writes
-- through (services/records-service/agent_lifecycle.py) — keyed by the
-- draft's own correlation_id, which is already stable across generation,
-- review, and every display. Only the same metadata TraceRecorder already
-- allows ever lands here (see libs/agent_provenance/recorder.py's
-- FORBIDDEN_KEYS guard, re-checked again at the point of insert): stage,
-- sequence, timestamps, correlation id, source/version/citation
-- references, categories, bounded counts/durations, provenance label,
-- model/prompt version, validation code, decision category, and display
-- version. No patient/user id, no actor name, no prompt, no response, no
-- draft/retrieved text, no credential, no raw provider error ever reaches
-- this table structurally, because none of those can ever reach the
-- StageEvent objects this table is fed from.
--
-- sequence IS NOT application-supplied. A BEFORE INSERT trigger assigns it,
-- serialized per correlation_id by a transaction-scoped advisory lock (the
-- same pattern migration 027's hash-chain trigger already uses for its own
-- global ordering) — this is what makes concurrent appends for the same
-- correlation_id (a genuinely concurrent request, or a route retry) both
-- monotonic and safe, without the caller ever computing or racing on "the
-- next number" itself.
--
-- APPEND-ONLY, same reasoning and same mechanism as migration 026's
-- audit_logs guard: ALTER DEFAULT PRIVILEGES (028) already grants
-- riverbend_app UPDATE/DELETE on every new table by default, so only a
-- BEFORE UPDATE/DELETE trigger — not a REVOKE — actually closes the gap.
-- This is tamper-resistant against the runtime role, not a hash chain; it
-- is not claimed to be tamper-evident against a database owner/superuser.

CREATE TABLE IF NOT EXISTS agent_lifecycle_events (
    id             BIGSERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    sequence       INTEGER NOT NULL,
    stage          TEXT NOT NULL
                   CHECK (stage IN ('request', 'provider_call', 'agent_decision',
                                    'retrieval', 'draft', 'validation', 'review',
                                    'display')),
    attributes     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_lifecycle_events_correlation_sequence_unique
    ON agent_lifecycle_events (correlation_id, sequence);
CREATE INDEX IF NOT EXISTS agent_lifecycle_events_correlation_id_idx
    ON agent_lifecycle_events (correlation_id);

-- Review fix ALC-DISPLAY-REPEAT: display is the trace's TERMINAL stage —
-- TraceRecorder.is_ordered() already rejects a repeat of it in-memory, but
-- that guards one request's own trace, not two genuinely concurrent
-- requests (two patient GETs of the same approved summary) each building
-- their OWN trace and both trying to persist a display event for the same
-- correlation_id. This partial unique index is the database-level
-- backstop: at most one display row can ever exist per correlation_id,
-- full stop. services/records-service/agent_lifecycle.py's persist()
-- catches the resulting UniqueViolation for a display insert specifically
-- and treats it as an already-recorded no-op, never a surfaced error —
-- concurrent reads must both succeed even though only one display row is
-- ever stored. No other stage is restricted this way: provider_call,
-- agent_decision and retrieval are all genuinely repeatable within one
-- bounded tool loop.
CREATE UNIQUE INDEX IF NOT EXISTS agent_lifecycle_events_one_display_per_correlation
    ON agent_lifecycle_events (correlation_id) WHERE stage = 'display';

CREATE OR REPLACE FUNCTION agent_lifecycle_events_assign_sequence() RETURNS TRIGGER AS $$
DECLARE
    next_seq integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('agent_lifecycle_events:' || NEW.correlation_id));
    SELECT COALESCE(MAX(sequence), 0) + 1 INTO next_seq
        FROM agent_lifecycle_events WHERE correlation_id = NEW.correlation_id;
    NEW.sequence := next_seq;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_lifecycle_events_sequence ON agent_lifecycle_events;
CREATE TRIGGER agent_lifecycle_events_sequence
    BEFORE INSERT ON agent_lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION agent_lifecycle_events_assign_sequence();

CREATE OR REPLACE FUNCTION agent_lifecycle_events_reject_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'agent_lifecycle_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_lifecycle_events_no_update ON agent_lifecycle_events;
CREATE TRIGGER agent_lifecycle_events_no_update
    BEFORE UPDATE ON agent_lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION agent_lifecycle_events_reject_mutation();

DROP TRIGGER IF EXISTS agent_lifecycle_events_no_delete ON agent_lifecycle_events;
CREATE TRIGGER agent_lifecycle_events_no_delete
    BEFORE DELETE ON agent_lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION agent_lifecycle_events_reject_mutation();

-- Review fix ALC-CORR-COLLISION: agent_draft_provenance.correlation_id was
-- never unique — before this PR, a caller-supplied X-Request-Id could be
-- reused verbatim as the lifecycle key (services/records-service/app.py's
-- two generation routes now always call new_correlation_id() instead, but
-- the database itself must refuse a duplicate too, not merely rely on the
-- application never sending one again). Every row this migration finds is
-- either pre-fix demo/seed data or a genuine bug; either way, silently
-- picking a "winner" among duplicates (or deleting/merging their
-- agent_lifecycle_events rows) is a real, human-reviewable decision this
-- migration must not make on its own — it fails loudly and touches zero
-- rows instead, the same posture migration 013 already established for
-- appointments.slot_id.
DO $$
DECLARE
    dup_count integer;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'agent_draft_provenance_correlation_id_unique'
    ) THEN
        SELECT count(*) INTO dup_count FROM (
            SELECT correlation_id FROM agent_draft_provenance
            GROUP BY correlation_id
            HAVING count(*) > 1
        ) dupes;

        IF dup_count > 0 THEN
            RAISE EXCEPTION 'agent_draft_provenance_correlation_id_unique: % correlation_id value(s) are shared by more than one draft. This migration will NOT silently join or delete lifecycle data. Identify and resolve each duplicate by hand (a pre-fix row that reused a caller-supplied X-Request-Id, or a genuine bug), then re-run this migration.', dup_count;
        END IF;

        CREATE UNIQUE INDEX agent_draft_provenance_correlation_id_unique
            ON agent_draft_provenance (correlation_id);
    END IF;
END
$$;
