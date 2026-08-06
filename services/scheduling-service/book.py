"""Appointment booking.

Stage 4 (Week 5, RIV-175, migration 013): this used to be a classic
check-then-act race — slot_taken() and insert_appointment() ran as two
separate connections/transactions, with an artificial time.sleep(0.05)
between them specifically to widen the race window for the demo. Two
near-simultaneous requests (or a client retry of a slow POST) could both
pass slot_taken() and both insert, with no UNIQUE constraint on slot_id and
no idempotency key to tell a retry apart from a second, distinct booking.

Fixed at the database level, not by tightening application-side timing:
migration 013 added a partial UNIQUE index (at most one 'confirmed'
appointment per slot_id) and a per-patient UNIQUE index on
(patient_id, idempotency_key). book() below does the idempotency check and
the insert in ONE transaction (same connection, no close() in between), and
uses a SAVEPOINT around the insert so a unique-violation only unwinds the
insert itself.

On a unique-violation, book() checks for its OWN (patient_id,
idempotency_key) row FIRST, before looking at which constraint the error
named — not the other way around. A single failed INSERT can violate BOTH
indexes at once (several concurrent replays of the same idempotency_key for
the same slot), and which one Postgres reports isn't something this code
controls; a 6-way concurrent-replay integration test
(tests/integration/test_scheduling_concurrency.py) caught the slot
constraint being reported for some racers even though every request shared
one key. If a row for this (patient_id, idempotency_key) exists now, it's
always a replay — return that SAME appointment_id, never a new one. Only
when no such row exists does the constraint name matter: a slot-index
violation there means a genuinely different booking won the slot first —
report slot_taken, same as before.
"""
import logging
import os
from typing import Optional

import psycopg2

log = logging.getLogger("scheduling-service")

_CONFIRMED_SLOT_CONSTRAINT = "appointments_confirmed_slot_unique"


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"),
        user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def _find_by_idempotency_key(cur, patient_id: int, idempotency_key: str) -> Optional[int]:
    cur.execute(
        "SELECT id FROM appointments WHERE patient_id = %s AND idempotency_key = %s",
        (patient_id, idempotency_key),
    )
    row = cur.fetchone()
    return row[0] if row else None


def book(
    patient_id: int,
    slot_id: int,
    idempotency_key: str,
    provider: Optional[str] = None,
    reason: Optional[str] = None,
    location: Optional[str] = None,
    scheduled_for=None,
) -> tuple[Optional[int], bool]:
    """Returns (appointment_id, is_replay).

    appointment_id is None only when the slot is genuinely already confirmed
    by a DIFFERENT booking (a real slot_taken) — never for a replay of this
    same idempotency_key, which always returns the original appointment_id
    with is_replay=True instead.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()

        # Pre-check: a caller retrying an already-completed booking with the
        # same key should never even attempt a second insert. This alone
        # doesn't close the race on its own (two near-simultaneous requests
        # with the same key can both pass this SELECT) — the SAVEPOINT below
        # is what makes that case safe too.
        existing_id = _find_by_idempotency_key(cur, patient_id, idempotency_key)
        if existing_id is not None:
            conn.commit()  # release the read transaction; nothing was written
            return existing_id, True

        cur.execute("SAVEPOINT before_insert")
        try:
            cur.execute(
                "INSERT INTO appointments "
                "(patient_id, slot_id, provider, reason, location, scheduled_for, status, idempotency_key) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'confirmed', %s) RETURNING id",
                (patient_id, slot_id, provider, reason, location, scheduled_for, idempotency_key),
            )
            appointment_id = cur.fetchone()[0]
            cur.execute("RELEASE SAVEPOINT before_insert")
            conn.commit()
            return appointment_id, False
        except psycopg2.errors.UniqueViolation as e:
            cur.execute("ROLLBACK TO SAVEPOINT before_insert")

            # Check for our OWN key first, regardless of which constraint
            # the error names. A single failed INSERT can violate BOTH
            # unique indexes at once — e.g. several concurrent replays of
            # the same idempotency_key for the same slot — and which one
            # Postgres reports first isn't something this code controls
            # (verified: a 6-way concurrent-replay integration test hit the
            # slot constraint being reported instead of the idempotency one
            # for some racers, even though every request shared one key).
            # If a row for (patient_id, idempotency_key) exists now, this is
            # always a replay, never a genuine slot conflict, no matter
            # which constraint fired.
            winner_id = _find_by_idempotency_key(cur, patient_id, idempotency_key)
            if winner_id is not None:
                conn.commit()
                log.info(
                    "booking idempotency replay detected under concurrency (patient_id=%s, slot_id=%s)",
                    patient_id, slot_id,
                )
                return winner_id, True

            constraint = getattr(e.diag, "constraint_name", None)
            if constraint == _CONFIRMED_SLOT_CONSTRAINT:
                conn.rollback()
                log.warning(
                    "booking rejected: slot already confirmed by another booking (patient_id=%s, slot_id=%s)",
                    patient_id, slot_id,
                )
                return None, False

            # An unexpected unique violation on some other constraint —
            # surface it rather than silently reporting slot_taken for a
            # failure this function doesn't actually understand.
            conn.rollback()
            raise
    finally:
        conn.close()
