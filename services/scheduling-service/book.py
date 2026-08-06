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

Round-22 review (2026-08-06): a "replay" is only ever safe when the retried
request actually matches the original — the first version of this file
found an existing (patient_id, idempotency_key) row and returned its id
unconditionally, so reusing a key for a DIFFERENT booking (a different
slot_id, most dangerously) silently returned someone's earlier, unrelated
appointment as if it were the one just requested. _fingerprint_matches
compares slot_id/provider/reason/location/scheduled_for against the
existing row before treating anything as a replay; a mismatch raises
IdempotencyKeyConflict instead.
"""
import logging
import os
from typing import Optional

import psycopg2

log = logging.getLogger("scheduling-service")

_CONFIRMED_SLOT_CONSTRAINT = "appointments_confirmed_slot_unique"


class IdempotencyKeyConflict(Exception):
    """Round-22 review (2026-08-06): raised when idempotency_key is reused
    for the same patient with DIFFERENT booking details than the original
    request — e.g. a different slot_id. The earlier version of book() only
    checked (patient_id, idempotency_key) and returned the existing
    appointment_id as a "successful replay" regardless of whether the rest
    of the request matched, so a reused key with a different slot silently
    returned someone's earlier, unrelated booking as if it were the one
    just requested. This is a genuine conflict the caller must resolve (use
    a new key, or resend the original request unchanged) — never a safe
    replay to confirm."""

    def __init__(self, existing_appointment_id: int):
        self.existing_appointment_id = existing_appointment_id
        super().__init__(
            f"idempotency_key already used for a different booking (existing appointment_id={existing_appointment_id})"
        )


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "postgres"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"),
        user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def _find_by_idempotency_key(cur, patient_id: int, idempotency_key: str):
    """Returns None, or (id, slot_id, provider, reason, location,
    scheduled_for) — the fields _fingerprint_matches compares a replay
    request against, not just the bare id."""
    cur.execute(
        "SELECT id, slot_id, provider, reason, location, scheduled_for FROM appointments "
        "WHERE patient_id = %s AND idempotency_key = %s",
        (patient_id, idempotency_key),
    )
    return cur.fetchone()


def _fingerprint_matches(existing_row, slot_id, provider, reason, location, scheduled_for) -> bool:
    _, existing_slot_id, existing_provider, existing_reason, existing_location, existing_scheduled_for = existing_row
    try:
        return (
            existing_slot_id == slot_id
            and existing_provider == provider
            and existing_reason == reason
            and existing_location == location
            and existing_scheduled_for == scheduled_for
        )
    except TypeError:
        # e.g. a naive vs timezone-aware datetime comparison raises rather
        # than returning False. Treat "cannot confirm this is the same
        # request" as a mismatch — the safer default given the alternative
        # is silently returning someone else's booking as a replay.
        return False


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
    with is_replay=True instead. Raises IdempotencyKeyConflict if
    idempotency_key was already used for a booking with DIFFERENT details —
    that is never treated as a safe replay.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()

        # Pre-check: a caller retrying an already-completed booking with the
        # same key should never even attempt a second insert. This alone
        # doesn't close the race on its own (two near-simultaneous requests
        # with the same key can both pass this SELECT) — the SAVEPOINT below
        # is what makes that case safe too.
        existing = _find_by_idempotency_key(cur, patient_id, idempotency_key)
        if existing is not None:
            existing_id = existing[0]
            conn.commit()  # release the read transaction; nothing was written
            if not _fingerprint_matches(existing, slot_id, provider, reason, location, scheduled_for):
                raise IdempotencyKeyConflict(existing_id)
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
            winner = _find_by_idempotency_key(cur, patient_id, idempotency_key)
            if winner is not None:
                winner_id = winner[0]
                conn.commit()
                if not _fingerprint_matches(winner, slot_id, provider, reason, location, scheduled_for):
                    raise IdempotencyKeyConflict(winner_id)
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
