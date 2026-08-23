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
compares slot_id/reason against the existing row before treating anything
as a replay; a mismatch raises IdempotencyKeyConflict instead.

w9-fixes P0 4.2/4.3 (2026-08-23): provider/location/scheduled_for used to be
whatever the CALLER sent, and `slots.status` was never updated on a
successful booking — so a slot that had just been confirmed kept appearing
"open" in GET /slots (the live demo DB already had at least twenty such
contradictions), and a booking's own displayed time/location/provider could
disagree with the slot the patient actually picked. Both are fixed the same
way: book() now locks the slot row (SELECT ... FOR UPDATE, same transaction
as the insert), requires status='open', and derives provider/location/
scheduled_for from THAT row — the caller no longer supplies any of the
three. A successful, non-replay insert also flips the slot to 'booked' in
the same transaction, so the read model (GET /slots) and the write model
(appointments) can never disagree about a slot this code touched. Cancel is
the other half — see app.py::cancel_appointment, which reopens the slot.
"""
import logging
import os
from typing import Optional

import psycopg2

log = logging.getLogger("scheduling-service")

_CONFIRMED_SLOT_CONSTRAINT = "appointments_confirmed_slot_unique"


class IdempotencyKeyConflict(Exception):
    """Round-22 review (2026-08-06): raised when idempotency_key is reused
    for the same patient with a DIFFERENT slot_id or reason than the
    original request — e.g. a different slot_id. The earlier version of
    book() only checked (patient_id, idempotency_key) and returned the
    existing appointment_id as a "successful replay" regardless of whether
    the rest of the request matched, so a reused key with a different slot
    silently returned someone's earlier, unrelated booking as if it were
    the one just requested. This is a genuine conflict the caller must
    resolve (use a new key, or resend the original request unchanged) —
    never a safe replay to confirm."""

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
    """Returns None, or (id, slot_id, reason) — the fields
    _fingerprint_matches compares a replay request against. provider/
    location/scheduled_for are no longer part of this: they are always
    derived from slot_id now, so comparing slot_id already covers them."""
    cur.execute(
        "SELECT id, slot_id, reason FROM appointments "
        "WHERE patient_id = %s AND idempotency_key = %s",
        (patient_id, idempotency_key),
    )
    return cur.fetchone()


def _fingerprint_matches(existing_row, slot_id, reason) -> bool:
    _, existing_slot_id, existing_reason = existing_row
    return existing_slot_id == slot_id and existing_reason == reason


def _lock_open_slot(cur, slot_id: int):
    """Locks and returns (provider_name, location, start_at) for slot_id, or
    None if it does not exist, is not currently 'open', or its start_at is
    not in the future. FOR UPDATE within book()'s single transaction: a
    second concurrent booking attempt for the SAME slot blocks here until
    the first transaction commits or rolls back, so it is impossible for two
    callers to both read 'open' and both proceed — the property GET /slots
    already relies on for its own "do not offer an occupied slot" filter
    (app.py::list_slots), which the start_at check mirrors here too: a slot
    can be status='open' with no confirmed appointment and still be wrong to
    book if its time has already passed."""
    # FOR UPDATE OF s, not a bare FOR UPDATE: Postgres refuses to lock a row
    # on the nullable side of an outer join (providers here, since
    # provider_id can theoretically be absent) — discovered against real
    # Postgres via the integration suite; the fake-cursor unit tests never
    # sent real SQL, so they couldn't catch it. Scoping the lock to s alone
    # is exactly what's needed: only the slot row itself has to be locked.
    cur.execute(
        "SELECT s.status, s.location, s.start_at, p.name "
        "FROM slots s LEFT JOIN providers p ON p.id = s.provider_id "
        "WHERE s.id = %s AND s.start_at > now() FOR UPDATE OF s",
        (slot_id,),
    )
    row = cur.fetchone()
    if row is None or row[0] != "open":
        return None
    _status, location, start_at, provider_name = row
    return provider_name, location, start_at


def book(
    patient_id: int,
    slot_id: int,
    idempotency_key: str,
    reason: Optional[str] = None,
) -> tuple[Optional[int], bool]:
    """Returns (appointment_id, is_replay).

    appointment_id is None when the slot is genuinely already confirmed by a
    DIFFERENT booking, does not exist, or is not currently 'open' — never
    for a replay of this same idempotency_key, which always returns the
    original appointment_id with is_replay=True instead. Raises
    IdempotencyKeyConflict if idempotency_key was already used for a
    booking with a DIFFERENT slot_id or reason — that is never treated as a
    safe replay.

    provider/location/scheduled_for are no longer parameters: they are
    always derived from the LOCKED slot row, never from the caller — see
    the module docstring.
    """
    conn = get_conn()
    try:
        cur = conn.cursor()

        # Pre-check: a caller retrying an already-completed booking with the
        # same key should never even attempt a second insert, and must never
        # touch (lock/re-derive from) the slot again either — the original
        # call already did that. This alone doesn't close the race on its
        # own (two near-simultaneous requests with the same key can both
        # pass this SELECT) — the SAVEPOINT below is what makes that case
        # safe too.
        existing = _find_by_idempotency_key(cur, patient_id, idempotency_key)
        if existing is not None:
            existing_id = existing[0]
            conn.commit()  # release the read transaction; nothing was written
            if not _fingerprint_matches(existing, slot_id, reason):
                raise IdempotencyKeyConflict(existing_id)
            return existing_id, True

        derived = _lock_open_slot(cur, slot_id)
        if derived is None:
            conn.rollback()
            # The pre-check above ran before this race resolved — the slot
            # may have JUST been won by a concurrent replay of this exact
            # idempotency_key (a different racer of the SAME retried
            # request), not a genuinely different booking. That must still
            # report success with the shared appointment_id, same as the
            # sequential-retry case the pre-check already handles, not
            # slot_taken. Discovered via a live 6-way concurrent-replay
            # integration test: with the slot lock in place, the loser of
            # the lock itself now resolves before the loser of the INSERT
            # used to (the UniqueViolation path below), so this check can no
            # longer be skipped just because that later path also has one.
            winner = _find_by_idempotency_key(cur, patient_id, idempotency_key)
            conn.commit()
            if winner is not None:
                winner_id = winner[0]
                if not _fingerprint_matches(winner, slot_id, reason):
                    raise IdempotencyKeyConflict(winner_id)
                log.info(
                    "booking idempotency replay detected after losing the slot lock race (patient_id=%s, slot_id=%s)",
                    patient_id, slot_id,
                )
                return winner_id, True
            log.info("slot %s not open or does not exist (patient=%s)", slot_id, patient_id)
            return None, False
        provider, location, scheduled_for = derived

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
            # Same transaction as the insert: a crash or error between these
            # two statements rolls both back together, so the read model
            # (slots.status) and the write model (appointments) can never
            # disagree about a slot this call touched.
            cur.execute("UPDATE slots SET status = 'booked' WHERE id = %s", (slot_id,))
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
                if not _fingerprint_matches(winner, slot_id, reason):
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
