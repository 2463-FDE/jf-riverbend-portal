"""The clinician review queue (S3) — the gate, and the queries behind it.

The client's requirement was explicit: the queue is not a table plus a screen.
The approve/reject decision has to control what the patient can actually see,
or the clinician half ships as decoration. So the read path for a patient's own
results consults this table, and everything here is written to fail closed.

Three rules this module exists to hold:

  * **Default deny.** `approved_record_ids` returns only ids with an explicit
    `approved` row. No row, `pending`, and `rejected` are all indistinguishable
    to the reader — they are simply absent from the set.
  * **A decision has an owner.** `decide` refuses to record one without an
    actor. Migration 018 enforces the same thing at the database level, so
    neither layer is trusted to be the only guard.
  * **Rejection is durable.** `enqueue_refusals` skips any record that already
    has a review row in ANY state. Without that, a rejected item would re-queue
    the next time the patient opened the page, and the clinician's decision
    would quietly mean nothing.
"""
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from models import PatientSummaryReview

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

_DECISIONS = frozenset({APPROVED, REJECTED})


def approved_record_ids(db: Session, patient_id: int) -> frozenset:
    """Record ids this patient is cleared to see refused content for.

    Deliberately returns a set of ids rather than a state map: the caller's
    only legitimate question is "may this be shown", and handing it richer
    state invites a future branch on `pending` that shows something.
    """
    rows = db.execute(
        select(PatientSummaryReview.record_id).where(
            PatientSummaryReview.patient_id == patient_id,
            PatientSummaryReview.state == APPROVED,
        )
    ).scalars().all()
    return frozenset(rows)


def enqueue_refusals(db: Session, patient_id: int, refused: Iterable[tuple]) -> int:
    """Queue refused results for review. Returns how many rows were created.

    `refused` is an iterable of (record_id, reason).

    Called from the patient's own read path, which makes this a write during a
    GET — a deliberate trade. A queue populated by some other trigger would
    drift from what patients are actually refused; this way the two are the
    same set by construction. Idempotent, so a refresh creates nothing, and it
    skips records already reviewed in any state (see the module docstring on
    why re-queueing a rejection would nullify it).
    """
    refused = list(refused)
    if not refused:
        return 0

    record_ids = [rid for rid, _ in refused]
    already = set(
        db.execute(
            select(PatientSummaryReview.record_id).where(
                PatientSummaryReview.record_id.in_(record_ids)
            )
        ).scalars().all()
    )

    created = 0
    for record_id, reason in refused:
        if record_id in already:
            continue
        db.add(
            PatientSummaryReview(
                patient_id=patient_id,
                record_id=record_id,
                state=PENDING,
                reason=reason,
            )
        )
        created += 1

    if created:
        try:
            db.flush()
        except SQLAlchemyError:
            # A concurrent reader may have queued the same record between the
            # SELECT above and this flush; the partial unique index catches it.
            # Losing the race is harmless — the row exists either way — so this
            # rolls back the additions rather than failing the patient's read.
            db.rollback()
            return 0
    return created


def pending_reviews(db: Session, limit: int = 100):
    """The queue, oldest first — work the backlog, not the newest arrival."""
    return db.execute(
        select(PatientSummaryReview)
        .where(PatientSummaryReview.state == PENDING)
        .order_by(PatientSummaryReview.created_at.asc(), PatientSummaryReview.id.asc())
        .limit(limit)
    ).scalars().all()


def decide(
    db: Session,
    *,
    review_id: int,
    state: str,
    actor_id: Optional[int],
    note: Optional[str] = None,
) -> Optional[PatientSummaryReview]:
    """Record an approve/reject decision. Returns the row, or None if it was
    not pending.

    Refuses without an actor: an approval is a named clinician taking
    responsibility for releasing chart content, and an anonymous one would
    make the accounting worthless. Refuses on a non-pending row so a decision
    cannot be silently overwritten by a second click or a stale screen.
    """
    if state not in _DECISIONS:
        raise ValueError(f"state must be one of {sorted(_DECISIONS)}, got {state!r}")
    if actor_id is None:
        raise ValueError("a decision requires an identified clinician")

    review = db.execute(
        select(PatientSummaryReview).where(
            PatientSummaryReview.id == review_id,
            PatientSummaryReview.state == PENDING,
        )
    ).scalar_one_or_none()
    if review is None:
        return None

    review.state = state
    review.decided_by = actor_id
    review.decided_at = func.now()
    review.decision_note = note
    db.flush()
    return review
