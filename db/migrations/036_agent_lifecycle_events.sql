-- 036_agent_lifecycle_events — W10 Final Stage 4.
--
-- Durable, append-only sink for the agent draft path's lifecycle events
-- (services/records-service/agent_lifecycle.py). Only what
-- libs/agent_provenance/recorder.py's FORBIDDEN_KEYS guard allows (checked
-- again at insert) ever lands in `attributes`. sequence is assigned by a
-- BEFORE INSERT trigger under an advisory lock (mirrors 027), never
-- application-supplied. APPEND-ONLY via BEFORE UPDATE/DELETE triggers,
-- same mechanism as 026's audit_logs guard (028 grants riverbend_app
-- UPDATE/DELETE by default, so a REVOKE can't close the gap) —
-- tamper-resistant against the runtime role, not against a superuser.

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

-- ALC-DISPLAY-REPEAT: display is the terminal stage; at most one row per
-- correlation_id, a DB-level backstop against two concurrent reads each
-- building their own trace. persist() treats the UniqueViolation as a no-op.
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

-- ALC-CORR-COLLISION: agent_draft_provenance.correlation_id was never
-- unique. A pre-existing duplicate is never silently joined/deleted; this
-- fails loudly instead, mirroring migration 013's posture for slot_id.
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
            RAISE EXCEPTION 'agent_draft_provenance_correlation_id_unique: % correlation_id value(s) are shared by more than one draft. This migration will NOT silently join or delete lifecycle data — resolve each duplicate by hand, then re-run.', dup_count;
        END IF;

        CREATE UNIQUE INDEX agent_draft_provenance_correlation_id_unique
            ON agent_draft_provenance (correlation_id);
    END IF;
END
$$;
