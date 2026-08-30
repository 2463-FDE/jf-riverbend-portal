"""Durable Bedrock chat usage accounting (migration 037) — the caller-side
sink for per-turn token usage each chat runtime (libs/summary_agent,
libs/policy_navigator) accumulates in memory and returns to its caller
(summary_agent_path.py, policy_navigator_path.py). Neither runtime writes
here directly; only what each already returns (provider, model id, a
bounded use-case category, token counts) ever reaches this table.
"""
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import BedrockUsageEvent


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
        row = BedrockUsageEvent(
            idempotency_key=f"{correlation_id}:{event.sequence}",
            provider=event.provider, model_id=event.model_id, use_case=event.use_case,
            input_tokens=event.input_tokens, output_tokens=event.output_tokens,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            continue
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
