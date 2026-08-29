"""Durable, append-only lifecycle event stream for the agent draft path
(migration 036). Generation, review, and display each used to build their
OWN in-memory `libs.agent_provenance.TraceRecorder`, discarded per
request; this module is the caller-side durable sink. Every write
re-checks `assert_safe` independently; sequence/append-only guarantees
live in the database trigger, never here.
"""
from typing import Iterable, List

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from libs.agent_provenance import Stage, StageEvent, TraceRecorder, assert_safe
from libs.tracing.spans import safe_span
from models import AgentLifecycleEvent

_TRACER_NAME = "records-service.agent_lifecycle"


def persist(db: Session, correlation_id: str, events: Iterable[StageEvent]) -> None:
    """Append already-recorded StageEvents to the durable table, in the SAME
    transaction as whatever else the caller is doing. The `sequence`
    computed here is a plain non-locking MAX+N read — valid for dialects
    without trigger support (SQLite), but not the concurrency guarantee:
    migration 036's trigger recomputes it under an advisory lock in real
    Postgres. Also emits through libs.tracing, best-effort — never
    completion evidence."""
    events = list(events)
    if not events:
        return
    current_max = db.execute(
        select(func.max(AgentLifecycleEvent.sequence)).where(
            AgentLifecycleEvent.correlation_id == correlation_id
        )
    ).scalar() or 0
    with safe_span(_TRACER_NAME, "agent_lifecycle.persist", {"correlation_id": correlation_id}) as span:
        for offset, event in enumerate(events, start=1):
            assert_safe(event.attributes)
            row = AgentLifecycleEvent(
                correlation_id=correlation_id,
                sequence=current_max + offset,
                stage=event.stage.value,
                attributes=dict(event.attributes),
            )
            if event.stage is Stage.DISPLAY:
                # ALC-DISPLAY-REPEAT (036's partial unique index): a
                # concurrent duplicate display is a no-op, not an error.
                try:
                    with db.begin_nested():
                        db.add(row)
                        db.flush()
                except IntegrityError:
                    continue
            else:
                db.add(row)
            span.add_event(event.stage.value, dict(event.attributes))
    db.flush()


def reconstruct(db: Session, correlation_id: str) -> TraceRecorder:
    """Read-only: rebuild one draft's lifecycle as a real TraceRecorder."""
    rows = db.execute(
        select(AgentLifecycleEvent)
        .where(AgentLifecycleEvent.correlation_id == correlation_id)
        .order_by(AgentLifecycleEvent.sequence)
    ).scalars().all()
    events: List[StageEvent] = [
        StageEvent(stage=Stage(row.stage), attributes=dict(row.attributes))
        for row in rows
    ]
    return TraceRecorder(correlation_id=correlation_id, events=events)
