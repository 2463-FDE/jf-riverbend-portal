-- 038_bedrock_usage_events_eligibility_use_case — W10 Metrics Stage 4.
--
-- Additive: widens migration 037's use_case CHECK to admit
-- 'eligibility_agent_chat', extending durable Bedrock usage accounting to
-- the third active paid online chat surface (services/eligibility-service/
-- bedrock_usage.py is its writer, following the exact idempotency-key and
-- append-only conventions 037 established for the first two). No column,
-- index, trigger, or existing row is touched — cost_usd/rate_version for
-- rows already written stay exactly as they were computed (see
-- libs/bedrock_pricing's "no retroactive backfill" rule).
--
-- The constraint name below was NOT guessed: it is Postgres's own
-- auto-generated name for 037's unnamed inline CHECK
-- (`{table}_{column}_check`), confirmed empirically against a throwaway
-- Postgres 15 instance before writing this migration.

ALTER TABLE bedrock_usage_events
    DROP CONSTRAINT bedrock_usage_events_use_case_check;

ALTER TABLE bedrock_usage_events
    ADD CONSTRAINT bedrock_usage_events_use_case_check
    CHECK (use_case IN ('summary_agent_chat', 'policy_navigator_chat', 'eligibility_agent_chat'));
