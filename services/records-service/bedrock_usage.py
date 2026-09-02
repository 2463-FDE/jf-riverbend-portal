"""Durable Bedrock chat usage accounting (migration 037) — the caller-side
sink for per-turn token usage each chat runtime (libs/summary_agent,
libs/policy_navigator) accumulates in memory and returns to its caller
(summary_agent_path.py, policy_navigator_path.py). Neither runtime writes
here directly; only what each already returns (provider, model id, a
bounded use-case category, token counts) ever reaches this table.

W10 Metrics Stage 4: `persist()` now computes `cost_usd`/`rate_version` via
libs/bedrock_pricing, populating them ONLY on an exact model_id rate match —
an unmatched model leaves both NULL (the table's own CHECK already requires
they travel together) and increments the bounded `rate_unavailable` metric,
never a guessed or zero cost. No historical row is ever touched: a rate
added later never repriced usage recorded before it existed.
"""
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from libs.bedrock_pricing import compute_cost
from libs.metrics import ai as ai_metrics
from models import BedrockUsageEvent

_IDEMPOTENCY_KEY_CONSTRAINT = "bedrock_usage_events_idempotency_key_unique"
_POSTGRES_UNIQUE_VIOLATION_SQLSTATE = "23505"
_SQLITE_IDEMPOTENCY_KEY_MESSAGE = "UNIQUE constraint failed: bedrock_usage_events.idempotency_key"


def _is_idempotency_key_duplicate(exc: IntegrityError) -> bool:
    """Review fix BU-ERR-SWALLOW: True ONLY for the exact idempotency_key
    UNIQUE violation persist() already expects as an idempotent retry —
    never a CHECK, NOT NULL, foreign-key, or any other integrity failure,
    all of which are real bugs and must propagate, not be silently
    swallowed alongside the one case that is genuinely a no-op."""
    orig = exc.orig
    diag = getattr(orig, "diag", None)
    if diag is not None:
        # PostgreSQL: pin to BOTH the unique_violation SQLSTATE and this
        # exact constraint's name, never any other unique constraint this
        # (or another) table might carry.
        return (
            getattr(diag, "sqlstate", None) == _POSTGRES_UNIQUE_VIOLATION_SQLSTATE
            and getattr(diag, "constraint_name", None) == _IDEMPOTENCY_KEY_CONSTRAINT
        )
    if isinstance(orig, sqlite3.IntegrityError):
        # SQLite carries no SQLSTATE or constraint name — only its own
        # fixed message shape naming the exact table.column.
        return _SQLITE_IDEMPOTENCY_KEY_MESSAGE in str(orig)
    return False


@dataclass(frozen=True)
class UsageEvent:
    """One model turn's token usage. `sequence` is the turn number within
    the run — combined with the caller's correlation_id, it is this row's
    idempotency key."""

    provider: str
    model_id: str
    use_case: str
    sequence: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


def persist(db: Session, correlation_id: str, events: Iterable[UsageEvent]) -> None:
    """Append usage rows, in the SAME transaction as whatever else the
    caller is doing. idempotency_key = f"{correlation_id}:{sequence}" —
    migration 037's unique index makes a retried write of the SAME turn a
    no-op: a SAVEPOINT scopes the failure to just this insert, the same
    pattern agent_lifecycle.py's persist() uses for ALC-DISPLAY-REPEAT."""
    for event in events:
        priced = compute_cost(event.model_id, event.input_tokens, event.output_tokens)
        if priced is not None:
            cost_usd, rate_version = priced
            ai_metrics.record_cost(model_id=event.model_id, use_case=event.use_case, cost_usd=cost_usd)
        else:
            cost_usd, rate_version = None, None
            if event.input_tokens is not None or event.output_tokens is not None:
                # A real call happened and reported usage, but no exact
                # versioned rate matches this model_id — visible as a
                # bounded metric, never silently dropped.
                ai_metrics.record_rate_unavailable(model_id=event.model_id, use_case=event.use_case)
        row = BedrockUsageEvent(
            idempotency_key=f"{correlation_id}:{event.sequence}",
            provider=event.provider, model_id=event.model_id, use_case=event.use_case,
            input_tokens=event.input_tokens, output_tokens=event.output_tokens,
            rate_version=rate_version, cost_usd=cost_usd,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError as exc:
            if not _is_idempotency_key_duplicate(exc):
                raise
    db.flush()


def usage_for(db: Session, *, model_id: Optional[str] = None, use_case: Optional[str] = None,
              since=None, until=None) -> list:
    """Read-only query by model/use case/time window. Never returns prompt,
    response, or any other content — none is ever stored in this table."""
    stmt = select(BedrockUsageEvent)
    if model_id is not None:
        stmt = stmt.where(BedrockUsageEvent.model_id == model_id)
    if use_case is not None:
        stmt = stmt.where(BedrockUsageEvent.use_case == use_case)
    if since is not None:
        stmt = stmt.where(BedrockUsageEvent.created_at >= since)
    if until is not None:
        stmt = stmt.where(BedrockUsageEvent.created_at < until)
    return list(db.execute(stmt.order_by(BedrockUsageEvent.created_at)).scalars().all())
