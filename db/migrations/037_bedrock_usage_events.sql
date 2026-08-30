-- 037_bedrock_usage_events — W10 Final Stage 5 sub-slice 3.
--
-- Durable, append-only usage accounting for the two active Bedrock CHAT
-- paths (summary_agent, policy_navigator) — services/records-service/
-- bedrock_usage.py is the only writer. Persists only what the stage asks
-- for: provider, model, a bounded use-case category, input/output token
-- counts, and timestamp. Never a prompt, response, caller question,
-- retrieved text, patient/user id, credential, or raw error — this table
-- has no column that could hold any of those.
--
-- cost_usd stays NULL until a real, explicit, versioned rate configuration
-- exists (none is added in this migration) — never guessed. rate_version
-- names which rate config a non-NULL cost_usd was computed under; both are
-- NULL together or set together (see the CHECK below).
--
-- idempotency_key is `<correlation_id>:<sequence>` — one row per model
-- turn, keyed the same way migration 036's lifecycle events are keyed by
-- (correlation_id, sequence), so a retried write is a no-op rather than a
-- duplicate charge-adjacent row (see bedrock_usage.py's persist()).
--
-- APPEND-ONLY via BEFORE UPDATE/DELETE triggers, same mechanism as
-- migration 026's audit_logs guard and migration 036's lifecycle events
-- (028 grants riverbend_app UPDATE/DELETE by default, so a REVOKE can't
-- close the gap) — tamper-resistant against the runtime role, not against
-- a superuser.

CREATE TABLE IF NOT EXISTS bedrock_usage_events (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    use_case        TEXT NOT NULL
                    CHECK (use_case IN ('summary_agent_chat', 'policy_navigator_chat')),
    input_tokens    INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens   INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    rate_version    TEXT,
    cost_usd        NUMERIC(12, 6) CHECK (cost_usd IS NULL OR cost_usd >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((rate_version IS NULL) = (cost_usd IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS bedrock_usage_events_idempotency_key_unique
    ON bedrock_usage_events (idempotency_key);
CREATE INDEX IF NOT EXISTS bedrock_usage_events_model_use_case_created_idx
    ON bedrock_usage_events (model_id, use_case, created_at);

CREATE OR REPLACE FUNCTION bedrock_usage_events_reject_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'bedrock_usage_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bedrock_usage_events_no_update ON bedrock_usage_events;
CREATE TRIGGER bedrock_usage_events_no_update
    BEFORE UPDATE ON bedrock_usage_events
    FOR EACH ROW EXECUTE FUNCTION bedrock_usage_events_reject_mutation();

DROP TRIGGER IF EXISTS bedrock_usage_events_no_delete ON bedrock_usage_events;
CREATE TRIGGER bedrock_usage_events_no_delete
    BEFORE DELETE ON bedrock_usage_events
    FOR EACH ROW EXECUTE FUNCTION bedrock_usage_events_reject_mutation();
