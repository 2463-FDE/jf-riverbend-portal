"""Durable Bedrock chat usage accounting for the eligibility assistant (W10
Metrics Stage 4) — migration 038 widened bedrock_usage_events' use_case
CHECK specifically so this surface could write here too.

This is this service's ONLY direct Postgres access; everything else goes
through gateway-fronted HTTP calls to other services. Raw psycopg2 rather
than an ORM: usage rows are low-frequency, the table's shape is owned by
records-service's migrations (037/038), and `ON CONFLICT ... DO NOTHING`
gives the same idempotent-retry semantics services/records-service/
bedrock_usage.py gets from its SAVEPOINT + catch, without introducing
SQLAlchemy for one table.

Best-effort by design, matching the review-driven precedent for this exact
kind of step (services/records-service/app.py's PN-FLUSH-ESCAPE comment): a
persistence failure must never affect the chat reply already produced.
Every public function catches broadly and logs by TYPE only, never raises.
"""
import logging
from typing import Iterable

from config import settings
from libs.bedrock_pricing import compute_cost
from libs.metrics import ai as ai_metrics

log = logging.getLogger(__name__)

_PROVIDER = "bedrock"
_USE_CASE = "eligibility_agent_chat"
_INSERT_SQL = """
    INSERT INTO bedrock_usage_events
        (idempotency_key, provider, model_id, use_case, input_tokens, output_tokens, rate_version, cost_usd)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (idempotency_key) DO NOTHING
"""


def _dsn() -> str:
    return (
        f"host={settings.db_host} port={settings.db_port} dbname={settings.db_name} "
        f"user={settings.db_user} password={settings.db_password}"
    )


def persist(correlation_id: str, usage_turns: Iterable) -> None:
    """Append one row per turn in `usage_turns` (each an object with
    `model_id`/`turn`/`input_tokens`/`output_tokens`, e.g.
    libs.eligibility_agent.contracts.UsageTurn) — idempotency_key is
    `f"{correlation_id}:{turn.turn}"`, the SAME convention
    records-service/bedrock_usage.py uses. A no-op for an empty/falsy
    iterable; never opens a connection for nothing to write.
    """
    rows = list(usage_turns)
    if not rows:
        return
    try:
        import psycopg2  # lazy — only this module in this service needs it

        conn = psycopg2.connect(_dsn())
        try:
            with conn:
                with conn.cursor() as cur:
                    for turn in rows:
                        priced = compute_cost(turn.model_id, turn.input_tokens, turn.output_tokens)
                        if priced is not None:
                            cost_usd, rate_version = priced
                            ai_metrics.record_cost(model_id=turn.model_id, use_case=_USE_CASE, cost_usd=cost_usd)
                        else:
                            cost_usd, rate_version = None, None
                            if turn.input_tokens is not None or turn.output_tokens is not None:
                                ai_metrics.record_rate_unavailable(model_id=turn.model_id, use_case=_USE_CASE)
                        cur.execute(_INSERT_SQL, (
                            f"{correlation_id}:{turn.turn}", _PROVIDER, turn.model_id, _USE_CASE,
                            turn.input_tokens, turn.output_tokens, rate_version, cost_usd,
                        ))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — accounting must never break the chat reply
        log.warning("eligibility usage accounting failed (error_type=%s)", type(exc).__name__)
