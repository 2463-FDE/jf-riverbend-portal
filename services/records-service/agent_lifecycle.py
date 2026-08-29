"""Durable, append-only lifecycle event stream for the agent draft path
(W10 Final Stage 4, migration 036).

Summary generation, clinician review, and approved display each used to
instantiate their OWN in-memory `libs.agent_provenance.TraceRecorder`,
scoped to a single HTTP request, and discard it when that request ended —
three separate, temporally disjoint objects, none of them ever persisted.
This module is the caller-side durable sink that library's own docstring
anticipated ("wiring it to... `agent_draft_provenance` is the caller's job,
and keeping it pure is what lets the guard be tested without
infrastructure"): `TraceRecorder` itself stays exactly as it was.

Every write re-checks `assert_safe` as a second, independent gate before
anything reaches the database — never trusting that a caller only ever
constructs events through the guarded recorder. The sequence/append-only
guarantees live in the database itself (migration 036's trigger); this
module never computes or supplies a sequence number.
"""
from typing import Iterable, List

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from libs.agent_provenance import Stage, StageEvent, TraceRecorder, assert_safe
from libs.tracing.spans import safe_span
from models import AgentLifecycleEvent

_TRACER_NAME = "records-service.agent_lifecycle"


def persist(db: Session, correlation_id: str, events: Iterable[StageEvent]) -> None:
    """Append already-recorded StageEvents to the durable table, in the SAME
    transaction as whatever else the caller is doing — commits or rolls
    back together with the draft/validation/review write it describes.

    The `sequence` computed here is a plain, non-locking MAX+N read — a
    reasonable value in the common case, but NOT what makes concurrent
    appends for the same correlation_id safe. Migration 036's BEFORE INSERT
    trigger recomputes and overwrites it unconditionally, under a
    transaction-scoped advisory lock keyed on correlation_id; that trigger,
    not this function, is the actual concurrency guarantee. This read only
    exists so dialects without trigger support (SQLite, in unit tests —
    this table's Postgres-specific trigger is only ever exercised against a
    real Postgres in integration tests) still get a valid, unique value.

    Also emits the same allowlisted attributes through the existing
    OpenTelemetry seam (libs.tracing), best-effort — see that module's own
    "degrades to no-op on any failure" contract. This is NOT completion
    evidence: the durable rows just written are. Losing the OTel emission
    (SDK absent, exporter down) must never affect what was just persisted.
    """
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
            db.add(AgentLifecycleEvent(
                correlation_id=correlation_id,
                sequence=current_max + offset,
                stage=event.stage.value,
                attributes=dict(event.attributes),
            ))
            span.add_event(event.stage.value, dict(event.attributes))
    db.flush()


def reconstruct(db: Session, correlation_id: str) -> TraceRecorder:
    """Read-only: rebuild one draft's lifecycle, in persisted sequence
    order, as a real TraceRecorder — the existing successful-path grammar
    (is_ordered/is_complete/is_grounded/is_acceptable) applies to it
    unchanged. A fallback/error lifecycle is a genuinely shorter shape (see
    TraceRecorder's own module docstring) and is not expected to satisfy
    is_acceptable()."""
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
