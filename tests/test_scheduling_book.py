"""Unit tests for services/scheduling-service/book.py — Stage 4 (Week 5,
RIV-175, migration 013).

Drives book() directly against a fake psycopg2 connection/cursor (no real
Postgres) that understands exactly the statement sequence book() issues:
an idempotency-key SELECT (id, slot_id, provider, reason, location,
scheduled_for — round-22 review: enough to fingerprint-check a replay, not
just its bare id), a SAVEPOINT, the INSERT, then either RELEASE SAVEPOINT +
commit (success) or ROLLBACK TO SAVEPOINT + a constraint-specific recovery
path. Mirrors this repo's existing fake-session style for other services,
adapted to raw psycopg2 since this module deliberately doesn't use the ORM
(see book.py's own docstring).
"""
import pytest
import psycopg2

from conftest import load_module

book_mod = load_module("services/scheduling-service/book.py", "scheduling_book")

_BOOKING = dict(provider="Dr. X", reason="Follow-up", location="Riverbend Main", scheduled_for=None)


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
        if s.startswith("SELECT id, slot_id, provider, reason, location, scheduled_for FROM appointments"):
            patient_id, idempotency_key = params
            self._last = self._state.select_idempotency(patient_id, idempotency_key)
        elif s == "SAVEPOINT before_insert":
            self._last = None
        elif s.startswith("INSERT INTO appointments"):
            self._last = self._state.insert(params)
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


def _row(appt_id, slot_id=88231, provider="Dr. X", reason="Follow-up", location="Riverbend Main", scheduled_for=None):
    return (appt_id, slot_id, provider, reason, location, scheduled_for)


class _FreshBookingState:
    """No existing row for any idempotency key; INSERT always succeeds."""

    def __init__(self, next_id=500):
        self.next_id = next_id
        self.inserted = []

    def select_idempotency(self, patient_id, idempotency_key):
        return None

    def insert(self, params):
        self.inserted.append(params)
        appointment_id = self.next_id
        self.next_id += 1
        return (appointment_id,)


class _ExistingIdempotencyKeyState:
    """A row already exists for (patient_id, idempotency_key) with the SAME
    booking details — the pre-check must short-circuit before any INSERT is
    attempted, and return that row as a valid replay."""

    def __init__(self, existing_row):
        self.existing_row = existing_row

    def select_idempotency(self, patient_id, idempotency_key):
        return self.existing_row

    def insert(self, params):
        raise AssertionError("insert must not be attempted when the idempotency pre-check finds an existing row")


class _SlotTakenState:
    """Pre-check finds nothing, but a DIFFERENT booking already confirmed
    this slot — the slot-uniqueness index fires on INSERT."""

    def select_idempotency(self, patient_id, idempotency_key):
        return None

    def insert(self, params):
        raise _FakeUniqueViolation("appointments_confirmed_slot_unique")


class _ConcurrentIdempotencyRaceState:
    """The exact race the SAVEPOINT exists for: the pre-check SELECT finds
    nothing (a concurrent request with the same key hasn't committed yet),
    but that request wins the INSERT race — the idempotency index fires,
    and the second SELECT (after ROLLBACK TO SAVEPOINT, same transaction)
    now sees the winner's committed row."""

    def __init__(self, winner_row):
        self.winner_row = winner_row
        self.select_calls = 0

    def select_idempotency(self, patient_id, idempotency_key):
        self.select_calls += 1
        if self.select_calls == 1:
            return None
        return self.winner_row

    def insert(self, params):
        raise _FakeUniqueViolation("appointments_idempotency_key_unique")


class _UnexpectedConstraintState:
    """A unique violation on some constraint book() doesn't know about —
    must propagate, not be silently reported as slot_taken."""

    def select_idempotency(self, patient_id, idempotency_key):
        return None

    def insert(self, params):
        raise _FakeUniqueViolation("some_other_constraint")


def test_fresh_booking_inserts_and_commits(monkeypatch):
    state = _FreshBookingState(next_id=500)
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc", provider="Dr. X")

    assert appointment_id == 500
    assert is_replay is False
    assert state.inserted == [(1042, 88231, "Dr. X", None, None, None, "key-abc")]
    assert conn.commit_count == 1
    assert conn.rollback_count == 0
    assert conn.closed is True


def test_idempotency_replay_with_matching_details_short_circuits_before_any_insert(monkeypatch):
    state = _ExistingIdempotencyKeyState(_row(42, slot_id=88231, **_BOOKING))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc", **_BOOKING)

    assert appointment_id == 42
    assert is_replay is True
    assert conn.rollback_count == 0


def test_idempotency_replay_with_a_different_slot_raises_conflict_not_a_false_replay(monkeypatch):
    # Round-22 review (2026-08-06): reusing a key for a DIFFERENT slot must
    # never silently return the original appointment as if it were the one
    # just requested.
    state = _ExistingIdempotencyKeyState(_row(42, slot_id=88231, **_BOOKING))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    with pytest.raises(book_mod.IdempotencyKeyConflict) as exc_info:
        book_mod.book(1042, 99999, "key-abc", **_BOOKING)  # same key, different slot_id

    assert exc_info.value.existing_appointment_id == 42


def test_idempotency_replay_with_a_different_reason_also_raises_conflict(monkeypatch):
    state = _ExistingIdempotencyKeyState(_row(42, slot_id=88231, **_BOOKING))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    mismatched = {**_BOOKING, "reason": "a completely different reason"}
    with pytest.raises(book_mod.IdempotencyKeyConflict):
        book_mod.book(1042, 88231, "key-abc", **mismatched)


def test_slot_taken_by_a_different_booking_returns_none_not_replay(monkeypatch):
    state = _SlotTakenState()
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc")

    assert appointment_id is None
    assert is_replay is False
    assert conn.rollback_count == 1
    assert conn.commit_count == 0


def test_concurrent_idempotency_race_returns_winners_id_as_replay(monkeypatch):
    state = _ConcurrentIdempotencyRaceState(_row(77, slot_id=88231, **_BOOKING))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    appointment_id, is_replay = book_mod.book(1042, 88231, "key-abc", **_BOOKING)

    assert appointment_id == 77
    assert is_replay is True
    # This IS a successful outcome (the booking exists, just not from this
    # call) — commit, not rollback.
    assert conn.commit_count == 1
    assert conn.rollback_count == 0


def test_concurrent_idempotency_race_with_mismatched_details_raises_conflict(monkeypatch):
    state = _ConcurrentIdempotencyRaceState(_row(77, slot_id=88231, **_BOOKING))
    conn = _FakeConn(state)
    monkeypatch.setattr(book_mod, "get_conn", lambda: conn)

    with pytest.raises(book_mod.IdempotencyKeyConflict) as exc_info:
        book_mod.book(1042, 55555, "key-abc", **_BOOKING)  # different slot than the winner's

    assert exc_info.value.existing_appointment_id == 77


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
