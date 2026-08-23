"""Unit tests for services/scheduling-service/book.py — Stage 4 (Week 5,
RIV-175, migration 013), extended by w9-fixes P0 4.2/4.3.

Drives book() directly against a fake psycopg2 connection/cursor (no real
Postgres) that understands exactly the statement sequence book() issues: an
idempotency-key SELECT (id, slot_id, reason — round-22 review: enough to
fingerprint-check a replay, not just its bare id), a locked SELECT of the
slot row (provider/location/scheduled_for are now derived from THIS, never
from the caller — w9-fixes P0 4.2/4.3), a SAVEPOINT, the INSERT, an UPDATE
that flips the slot to 'booked', then either RELEASE SAVEPOINT + commit
(success) or ROLLBACK TO SAVEPOINT + a constraint-specific recovery path.
Mirrors this repo's existing fake-session style for other services, adapted
to raw psycopg2 since this module deliberately doesn't use the ORM (see
book.py's own docstring).
"""
import pytest
import psycopg2

from conftest import load_module

book_mod = load_module("services/scheduling-service/book.py", "scheduling_book")

# What a locked, open slot looks like: (status, location, start_at, provider_name).
_OPEN_SLOT = ("open", "Riverbend Main", None, "Dr. X")
_TAKEN_SLOT = ("booked", "Riverbend Main", None, "Dr. X")


class _FakeUniqueViolation(psycopg2.errors.UniqueViolation):
    """psycopg2.errors.UniqueViolation's real .diag is a read-only C-backed
    Diagnostics object — not settable on a normally-constructed instance.
    Overriding diag as a property on a subclass works, and
    `except psycopg2.errors.UniqueViolation` still catches it (verified:
    isinstance holds), so this behaves exactly like an error surfaced by a
    real Postgres unique-index violation."""

    def __init__(self, constraint_name):
        super().__init__("simulated unique violation")
        self._constraint_name = constraint_name

    @property
    def diag(self):
        return type("Diag", (), {"constraint_name": self._constraint_name})()


class _FakeCursor:
    def __init__(self, state):
        self._state = state
        self._last = None

    def execute(self, sql, params=None):
        s = sql.strip()
        if s.startswith("SELECT id, slot_id, reason FROM appointments"):
            patient_id, idempotency_key = params
            self._last = self._state.select_idempotency(patient_id, idempotency_key)
        elif s.startswith("SELECT s.status, s.location, s.start_at, p.name"):
            (slot_id,) = params
            self._last = self._state.lock_slot(slot_id)
        elif s == "SAVEPOINT before_insert":
            self._last = None
        elif s.startswith("INSERT INTO appointments"):
            self._last = self._state.insert(params)
        elif s.startswith("UPDATE slots SET status = 'booked'"):
            self._state.slot_booked = params[0]
            self._last = None
        elif s in ("RELEASE SAVEPOINT before_insert", "ROLLBACK TO SAVEPOINT before_insert"):
            self._last = None
        else:
            raise AssertionError(f"unexpected SQL executed: {s!r}")

    def fetchone(self):
        return self._last


class _FakeConn:
    def __init__(self, state):
        self._state = state
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._state)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


def _row(appt_id, slot_id=88231, reason="Follow-up"):
    return (appt_id, slot_id, reason)


class _FreshBookingState:
    """No existing row for any idempotency key; the slot is open; INSERT
    always succeeds."""

    def __init__(self, next_id=500, slot=_OPEN_SLOT):
        self.next_id = next_id
        self.inserted = []
        self.slot = slot
        self.slot_booked = None  # set to the slot_id the UPDATE targeted, if any

    def select_idempotency(self, patient_id, idempotency_key):
        return None

    def lock_slot(self, slot_id):
        return self.slot

    def insert(self, params):
        self.inserted.append(params)
        appointment_id = self.next_id
        self.next_id += 1
        return (appointment_id,)


class _ExistingIdempotencyKeyState:
    """A row already exists for (patient_id, idempotency_key) with the SAME
    booking details — the pre-check must short-circuit before any slot lock
    or INSERT is attempted, and return that row as a valid replay."""

    def __init__(self, existing_row):
        self.existing_row = existing_row

    def select_idempotency(self, patient_id, idempotency_key):
        return self.existing_row

    def lock_slot(self, slot_id):
        raise AssertionError("the slot must not be locked when the idempotency pre-check finds an existing row")

    def insert(self, params):
        raise AssertionError("insert must not be attempted when the idempotency pre-check finds an existing row")


class _SlotNotOpenState:
    """Pre-check finds nothing, but the slot itself is not open (or does not
    exist) — book() must refuse before ever attempting an insert."""

    def __init__(self, slot=None):
        self.slot = slot  # None simulates "no such slot"

    def select_idempotency(self, patient_id, idempotency_key):
        return None

    def lock_slot(self, slot_id):
        return self.slot

    def insert(self, params):
        raise AssertionError("insert must not be attempted when the slot is not open")


class _SlotTakenState:
    """Pre-check finds nothing, the slot LOOKS open, but a DIFFERENT booking
    already confirmed it in the instant between — the slot-uniqueness index
    fires on INSERT (the race this module's SAVEPOINT design exists for)."""

    def __init__(self, slot=_OPEN_SLOT):
        self.slot = slot

    def select_idempotency(self, patient_id, idempotency_key):
        return None

    def lock_slot(self, slot_id):
        return self.slot

    def insert(self, params):
        raise _FakeUniqueViolation("appointments_confirmed_slot_unique")


class _ConcurrentIdempotencyRaceState:
    """The exact race the SAVEPOINT exists for: the pre-check SELECT finds
    nothing (a concurrent request with the same key hasn't committed yet),
    but that request wins the INSERT race — the idempotency index fires,
    and the second SELECT (after ROLLBACK TO SAVEPOINT, same transaction)
    now sees the winner's committed row."""

    def __init__(self, winner_row, slot=_OPEN_SLOT):
        self.winner_row = winner_row
        self.slot = slot
        self.select_calls = 0

    def select_idempotency(self, patient_id, idempotency_key):
        self.select_calls += 1
        if self.select_calls == 1:
            return None
        return self.winner_row

    def lock_slot(self, slot_id):
        return self.slot

    def insert(self, params):
        raise _FakeUniqueViolation("appointments_idempotency_key_unique")


class _LostSlotLockRaceState:
    """w9-fixes P0 4.2/4.3 follow-up (found via a live 6-way concurrent-
    replay integration test): the slot LOCK, not just the INSERT, is now
    something a concurrent replay of the SAME idempotency_key can win
    first, because _lock_open_slot runs before the INSERT ever happens. The
    pre-check SELECT above finds nothing (the winner hasn't committed yet),
    the slot then comes back not-open once the winner's transaction commits
    and this call's lock attempt unblocks, and a SECOND idempotency SELECT
    (after rolling back the failed lock attempt) is what has to recognize
    the winner as OUR OWN key rather than a stranger's booking — otherwise
    this call reports slot_taken for a request that actually succeeded."""

    def __init__(self, winner_row):
        self.winner_row = winner_row
        self.select_calls = 0

    def select_idempotency(self, patient_id, idempotency_key):
        self.select_calls += 1
        if self.select_calls == 1:
            return None
        return self.winner_row

    def lock_slot(self, slot_id):
        return None  # already booked by the winner by the time our lock resolves

    def insert(self, params):
        raise AssertionError("insert must not be attempted once the slot lock reports not-open")


class _UnexpectedConstraintState:
    """A unique violation on some constraint book() doesn't know about —
    must propagate, not be silently reported as slot_taken."""

    def select_idempotency(self, patient_id, idempotency_key):
        return None

    def lock_slot(self, slot_id):
        return _OPEN_SLOT

    def insert(self, params):
        raise _FakeUniqueViolation("some_other_constraint")


def test_fresh_booking_inserts_derives_from_the_slot_and_marks_it_booked(monkeypatch):
    state = _FreshBookingState(next_id=500, slot=("open", "Riverbend Main", None, "Dr. X"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc", reason="Follow-up")

    assert appointment_id == 500
    assert is_replay is False
    # provider/location/scheduled_for came from the LOCKED SLOT, never a
    # caller-supplied value — book() no longer even accepts those as params.
    assert state.inserted == [(1042, 88231, "Dr. X", "Follow-up", "Riverbend Main", None, "key-abc")]
    assert state.slot_booked == 88231, "a fresh confirmed booking must flip its slot to 'booked'"
    assert conn.commit_count == 1
    assert conn.rollback_count == 0
    assert conn.closed is True


def test_a_slot_that_is_not_open_is_refused_before_any_insert(monkeypatch):
    state = _SlotNotOpenState(slot=("booked", "Riverbend Main", None, "Dr. X"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc")

    assert appointment_id is None
    assert is_replay is False
    assert conn.rollback_count == 1  # the failed lock attempt
    # w9-fixes P0 4.2/4.3 follow-up: a not-open slot always triggers a
    # post-lock idempotency re-check now (see _LostSlotLockRaceState's own
    # docstring) — that read-only re-check commits even when, as here, it
    # finds no winner for this key either.
    assert conn.commit_count == 1


def test_a_nonexistent_slot_is_refused_the_same_way_as_a_taken_one(monkeypatch):
    state = _SlotNotOpenState(slot=None)
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 999999, "key-abc")

    assert appointment_id is None
    assert is_replay is False


def test_idempotency_replay_with_matching_details_short_circuits_before_touching_the_slot(monkeypatch):
    state = _ExistingIdempotencyKeyState(_row(42, slot_id=88231, reason="Follow-up"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc", reason="Follow-up")

    assert appointment_id == 42
    assert is_replay is True
    assert conn.rollback_count == 0


def test_idempotency_replay_with_a_different_slot_raises_conflict_not_a_false_replay(monkeypatch):
    # Round-22 review (2026-08-06): reusing a key for a DIFFERENT slot must
    # never silently return the original appointment as if it were the one
    # just requested.
    state = _ExistingIdempotencyKeyState(_row(42, slot_id=88231, reason="Follow-up"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    with pytest.raises(book_mod.IdempotencyKeyConflict) as exc_info:
        book_mod.book(1042, 99999, "key-abc", reason="Follow-up")  # same key, different slot_id

    assert exc_info.value.existing_appointment_id == 42


def test_idempotency_replay_with_a_different_reason_also_raises_conflict(monkeypatch):
    state = _ExistingIdempotencyKeyState(_row(42, slot_id=88231, reason="Follow-up"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    with pytest.raises(book_mod.IdempotencyKeyConflict):
        book_mod.book(1042, 88231, "key-abc", reason="a completely different reason")


def test_slot_taken_by_a_different_booking_returns_none_not_replay(monkeypatch):
    state = _SlotTakenState()
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc")

    assert appointment_id is None
    assert is_replay is False
    # Slot LOOKED open (the lock itself succeeded) — this is the INSERT-time
    # UniqueViolation path, not the pre-insert lock-race path above, so
    # there is no post-lock re-check to commit here.
    assert conn.rollback_count == 1
    assert conn.commit_count == 0


def test_concurrent_idempotency_race_returns_winners_id_as_replay(monkeypatch):
    state = _ConcurrentIdempotencyRaceState(_row(77, slot_id=88231, reason="Follow-up"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc", reason="Follow-up")

    assert appointment_id == 77
    assert is_replay is True
    # This IS a successful outcome (the booking exists, just not from this
    # call) — commit, not rollback.
    assert conn.commit_count == 1
    assert conn.rollback_count == 0


def test_concurrent_idempotency_race_with_mismatched_details_raises_conflict(monkeypatch):
    state = _ConcurrentIdempotencyRaceState(_row(77, slot_id=88231, reason="Follow-up"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    with pytest.raises(book_mod.IdempotencyKeyConflict) as exc_info:
        book_mod.book(1042, 55555, "key-abc", reason="Follow-up")  # different slot than the winner's

    assert exc_info.value.existing_appointment_id == 77


def test_losing_the_slot_lock_race_to_your_own_replay_reports_success_not_slot_taken(monkeypatch):
    state = _LostSlotLockRaceState(_row(88, slot_id=88231, reason="Follow-up"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc", reason="Follow-up")

    assert appointment_id == 88
    assert is_replay is True
    assert conn.commit_count == 1
    assert conn.rollback_count == 1  # the failed lock attempt itself, not the whole call


def test_losing_the_slot_lock_race_with_mismatched_details_raises_conflict(monkeypatch):
    state = _LostSlotLockRaceState(_row(88, slot_id=88231, reason="Follow-up"))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    with pytest.raises(book_mod.IdempotencyKeyConflict) as exc_info:
        book_mod.book(1042, 88231, "key-abc", reason="a completely different reason")

    assert exc_info.value.existing_appointment_id == 88


def test_unexpected_unique_violation_propagates_instead_of_reporting_slot_taken(monkeypatch):
    state = _UnexpectedConstraintState()
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    with pytest.raises(psycopg2.errors.UniqueViolation):
        book_mod.book(1042, 88231, "key-abc")

    assert conn.rollback_count == 1


def test_connection_is_always_closed_even_on_slot_taken(monkeypatch):
    conn = _FakeConn(_SlotTakenState())
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    book_mod.book(1042, 88231, "key-abc")

    assert conn.closed is True
