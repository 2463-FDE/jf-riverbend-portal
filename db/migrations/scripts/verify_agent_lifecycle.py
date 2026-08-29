#!/usr/bin/env python3
"""Read-only diagnostic: reconstruct one draft's durable lifecycle trace
(migration 036) and report it against the grammar in
`libs.agent_provenance.TraceRecorder`.

Usage:  DATABASE_URL=postgresql://... python3 verify_agent_lifecycle.py <correlation_id>

Prints stage names, counts, and boolean verdicts
(is_complete/is_ordered/is_grounded/is_acceptable) — never attribute
values, mirroring verify_audit_chain.py's posture for audit_logs.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from libs.agent_provenance import STAGES, Stage, StageEvent, TraceRecorder  # noqa: E402


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL must be set.", file=sys.stderr)
        raise SystemExit(2)
    return psycopg2.connect(dsn)


def reconstruct(conn, correlation_id: str) -> TraceRecorder:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stage, attributes FROM agent_lifecycle_events "
            "WHERE correlation_id = %s ORDER BY sequence",
            (correlation_id,),
        )
        rows = cur.fetchall()
    events = [StageEvent(stage=Stage(stage), attributes=dict(attributes or {})) for stage, attributes in rows]
    return TraceRecorder(correlation_id=correlation_id, events=events)


def report(trace: TraceRecorder) -> str:
    lines = [
        f"correlation_id: {trace.correlation_id}",
        f"stage count: {len(trace.events)}",
        f"stage sequence: {' -> '.join(e.stage.value for e in trace.events) or '(none)'}",
        f"is_complete: {trace.is_complete()}",
        f"is_ordered: {trace.is_ordered()}",
        f"is_grounded: {trace.is_grounded()}",
        f"is_acceptable (real/grounded path only): {trace.is_acceptable()}",
    ]
    if not trace.is_complete():
        lines.append(f"missing stages: {trace.missing_stages()}")
    if not trace.is_acceptable():
        lines.append(
            "note: a fallback/error lifecycle is a genuinely shorter shape "
            "and is not expected to satisfy is_acceptable()."
        )
    return "\n".join(lines)


def main(argv):
    if len(argv) != 2:
        print(f"usage: {argv[0]} <correlation_id>", file=sys.stderr)
        return 2
    correlation_id = argv[1]
    conn = _connect()
    try:
        trace = reconstruct(conn, correlation_id)
    finally:
        conn.close()
    if not trace.events:
        print(f"no events found for correlation_id={correlation_id!r}")
        return 1
    print(report(trace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
