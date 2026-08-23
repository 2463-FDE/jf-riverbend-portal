"""Secure patient-clinician messaging (W9.2) — schema-backed, append-only.

Deliberately not the eligibility chat's transient in-memory conversation: a
thread here is a real, persisted record with one immutable sender and
timestamp per message. There is no edit or delete route on `ThreadMessage` at
any layer, and there must not be one — see migration 022's own comments.

Authorization is not this module's job. Every function here takes the
patient_id (or a pre-authorized thread row) as given; `app.py`'s routes run
`_authorize_or_deny` — the same per-(actor, patient) grant check every other
chart-adjacent route in this service already uses — before calling in.
Fetching a thread by id here always filters by the caller's authorized
patient set in the SAME query as the id lookup, so an unauthorized thread id
and a nonexistent one return the identical `None` (see `thread_for_actor`).
"""
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from models import MessageThread, ThreadMessage, ThreadReadState, User, Patient

OPEN = "open"
CLOSED = "closed"
_STATUSES = frozenset({OPEN, CLOSED})


class MessagingError(Exception):
    """A domain rule was violated — the route turns this into a 409/400,
    never a 500. Never carries a message body or any other PHI-shaped text."""


def _display_name(user: Optional[User]) -> Optional[str]:
    if user is None:
        return None
    return user.full_name or user.username


def create_thread(
    db: Session,
    *,
    patient_id: int,
    sender_user_id: int,
    subject: str,
    body: str,
    idempotency_key: str,
) -> MessageThread:
    """A patient starts a new thread with their care team. The first message
    is created in the same transaction as the thread — there is no thread
    with zero messages in this model.

    Idempotent on (sender_user_id, idempotency_key): a retried create with the
    same key returns the SAME thread rather than opening a second one for an
    identical request.
    """
    existing = db.execute(
        select(ThreadMessage).where(
            ThreadMessage.sender_user_id == sender_user_id,
            ThreadMessage.idempotency_key == idempotency_key,
        )
    ).scalars().first()
    if existing is not None:
        return db.get(MessageThread, existing.thread_id)

    thread = MessageThread(patient_id=patient_id, subject=subject, created_by=sender_user_id)
    db.add(thread)
    db.flush()  # assigns thread.id for the message FK below

    db.add(
        ThreadMessage(
            thread_id=thread.id,
            sender_user_id=sender_user_id,
            body=body,
            idempotency_key=idempotency_key,
        )
    )
    return thread


def send_message(
    db: Session,
    *,
    thread: MessageThread,
    sender_user_id: int,
    body: str,
    idempotency_key: str,
) -> ThreadMessage:
    """Reply to an existing (already-authorized) thread.

    Idempotent the same way create_thread is: a replayed (sender, key) pair
    returns the original message rather than creating a second one, even if
    the thread has since closed — a replay is not a new send.

    Scoped to THIS thread as well as (sender, key) — round-1 review
    (MSG-002): a sender-only key meant reusing the same key against a
    DIFFERENT thread returned the first thread's message as if it had just
    been posted to the second, a false "success" for a reply that was never
    recorded where the caller actually asked for it. `thread_messages`'s own
    unique index is scoped the same way (migration 022).
    """
    existing = db.execute(
        select(ThreadMessage).where(
            ThreadMessage.sender_user_id == sender_user_id,
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.idempotency_key == idempotency_key,
        )
    ).scalars().first()
    if existing is not None:
        return existing

    if thread.status == CLOSED:
        raise MessagingError("this thread is closed; reopen it before replying")

    message = ThreadMessage(
        thread_id=thread.id,
        sender_user_id=sender_user_id,
        body=body,
        idempotency_key=idempotency_key,
    )
    db.add(message)
    thread.updated_at = func.now()
    return message


def set_status(db: Session, *, thread: MessageThread, status: str) -> MessageThread:
    if status not in _STATUSES:
        raise MessagingError(f"status must be one of {sorted(_STATUSES)}")
    thread.status = status
    thread.updated_at = func.now()
    return thread


def thread_for_actor(db: Session, *, thread_id: int, authorized_patient_ids) -> Optional[MessageThread]:
    """The thread, or None — identically, whether it does not exist or the
    caller holds no grant for its patient. `authorized_patient_ids` is a
    SELECT (patient_access_gate.active_patient_ids_query), embedded in the
    WHERE so an unauthorized row is never loaded to be checked afterward."""
    return db.execute(
        select(MessageThread).where(
            MessageThread.id == thread_id,
            MessageThread.patient_id.in_(authorized_patient_ids),
        )
    ).scalars().first()


def mark_read(db: Session, *, thread_id: int, user_id: int, last_message_id: int) -> None:
    row = db.get(ThreadReadState, (thread_id, user_id))
    if row is None:
        db.add(ThreadReadState(thread_id=thread_id, user_id=user_id, last_read_message_id=last_message_id))
    elif row.last_read_message_id is None or row.last_read_message_id < last_message_id:
        row.last_read_message_id = last_message_id
        row.updated_at = func.now()


def messages_for(db: Session, thread_id: int) -> list[dict]:
    """Ordered messages with the sender's display name resolved — the ONE
    query every thread-detail read uses, staff or patient."""
    rows = db.execute(
        select(ThreadMessage, User)
        .join(User, User.id == ThreadMessage.sender_user_id)
        .where(ThreadMessage.thread_id == thread_id)
        .order_by(ThreadMessage.created_at.asc(), ThreadMessage.id.asc())
    ).all()
    return [
        {
            "id": m.id,
            "thread_id": m.thread_id,
            "sender_user_id": m.sender_user_id,
            "sender_name": _display_name(u) or "Unknown",
            "body": m.body,
            "created_at": m.created_at.isoformat() if m.created_at else "",
        }
        for m, u in rows
    ]


def thread_summaries(db: Session, *, patient_ids, viewer_user_id: int, limit: int = 50) -> list[dict]:
    """Inbox rows for either audience: a patient's own single-row grant set,
    or every patient_id a clinician is granted. `patient_ids` is a SELECT
    (patient_access_gate.active_patient_ids_query), embedded directly in the
    WHERE below rather than materialized here — the same "authorization
    happens IN the SQL" property every other grant-scoped listing in this
    service relies on (see review_queue.pending_reviews). Unread is PER
    VIEWER — the same thread can show unread for one clinician and read for
    another, so this always joins thread_read_state on viewer_user_id
    specifically, never a shared "seen" flag on the thread itself."""
    threads = db.execute(
        select(MessageThread, Patient.name)
        .join(Patient, Patient.id == MessageThread.patient_id)
        .where(MessageThread.patient_id.in_(patient_ids))
        .order_by(MessageThread.updated_at.desc())
        .limit(limit)
    ).all()

    out = []
    for thread, patient_name in threads:
        last = db.execute(
            select(ThreadMessage, User)
            .join(User, User.id == ThreadMessage.sender_user_id)
            .where(ThreadMessage.thread_id == thread.id)
            .order_by(ThreadMessage.created_at.desc(), ThreadMessage.id.desc())
            .limit(1)
        ).first()
        last_message, last_sender = last if last else (None, None)

        read_row = db.get(ThreadReadState, (thread.id, viewer_user_id))
        last_read_id = read_row.last_read_message_id if read_row else None
        unread = db.execute(
            select(func.count(ThreadMessage.id)).where(
                ThreadMessage.thread_id == thread.id,
                ThreadMessage.sender_user_id != viewer_user_id,
                ThreadMessage.id > (last_read_id or 0),
            )
        ).scalar_one()

        out.append(
            {
                "id": thread.id,
                "patient_id": thread.patient_id,
                "patient_name": patient_name,
                "subject": thread.subject,
                "status": thread.status,
                "last_sender_name": _display_name(last_sender),
                "last_message_at": last_message.created_at.isoformat() if last_message else None,
                "unread_count": unread,
            }
        )
    return out
