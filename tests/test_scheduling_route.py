"""Route-level tests for services/scheduling-service/app.py::create_appointment
— Stage 4 (Week 5, RIV-175, migration 013).

Drives the real FastAPI route via TestClient with app_mod.book monkeypatched
directly (book.py's own behavior is covered in isolation by
tests/test_scheduling_book.py) — this file is about the route's contract:
idempotency_key is required, and book()'s (appointment_id, is_replay) tuple
maps to the right HTTP response.
"""
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/scheduling-service/app.py", "scheduling_app")


def _client():
    return TestClient(app_mod.app)


def _payload(**overrides):
    payload = {"patient_id": 1042, "slot_id": 88231, "idempotency_key": "test-key-1"}
    payload.update(overrides)
    return payload


def test_fresh_booking_returns_201_confirmed(monkeypatch):
    monkeypatch.setattr(app_mod, "book", lambda *a, **k: (123, False))

    resp = _client().post("/appointments", json=_payload())

    assert resp.status_code == 201
    assert resp.json() == {"appointment_id": 123, "status": "confirmed"}


def test_idempotent_replay_also_returns_201_confirmed_with_the_original_id(monkeypatch):
    # A retry of a slow POST must look identical to the caller as the
    # original success — never a failure, never a second appointment_id.
    monkeypatch.setattr(app_mod, "book", lambda *a, **k: (123, True))

    resp = _client().post("/appointments", json=_payload())

    assert resp.status_code == 201
    assert resp.json() == {"appointment_id": 123, "status": "confirmed"}


def test_slot_taken_by_a_different_booking_returns_slot_taken(monkeypatch):
    monkeypatch.setattr(app_mod, "book", lambda *a, **k: (None, False))

    resp = _client().post("/appointments", json=_payload())

    assert resp.status_code == 201  # status_code is fixed on the route; body carries the outcome
    assert resp.json() == {"appointment_id": None, "status": "slot_taken"}


def test_book_raising_returns_503_not_a_bare_500(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(app_mod, "book", _raise)

    resp = _client().post("/appointments", json=_payload())

    assert resp.status_code == 503


def test_idempotency_key_is_required():
    resp = _client().post(
        "/appointments",
        json={"patient_id": 1042, "slot_id": 88231},
    )
    assert resp.status_code == 422


def test_idempotency_key_forwarded_to_book(monkeypatch):
    captured = {}

    def _fake_book(patient_id, slot_id, idempotency_key, **kwargs):
        captured["idempotency_key"] = idempotency_key
        return (1, False)

    monkeypatch.setattr(app_mod, "book", _fake_book)

    _client().post("/appointments", json=_payload(idempotency_key="specific-key-xyz"))

    assert captured["idempotency_key"] == "specific-key-xyz"
