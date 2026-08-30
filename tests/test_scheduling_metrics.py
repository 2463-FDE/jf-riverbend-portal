"""W10 Final Stage 6 sub-slice 3 — services/scheduling-service/app.py's
SCHEDULING_BOOKING_OUTCOMES counter increments exactly once per business
outcome (success, conflict — both its code paths — retry, and failure).

Drives the real route via TestClient with app_mod.book monkeypatched
directly, mirroring tests/test_scheduling_route.py's own pattern exactly.
"""
from fastapi.testclient import TestClient

from conftest import load_module
from libs.metrics.business import SCHEDULING_BOOKING_OUTCOMES

app_mod = load_module("services/scheduling-service/app.py", "scheduling_metrics_app")

TEST_INTERNAL_TOKEN = "t" * 32


def _outcome_count(outcome):
    return SCHEDULING_BOOKING_OUTCOMES.labels(outcome=outcome)._value.get()


def _client(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)
    client = TestClient(app_mod.app)
    client.headers.update({"X-Internal-Token": TEST_INTERNAL_TOKEN})
    return client


def _payload(**overrides):
    payload = {"patient_id": 1042, "slot_id": 88231, "idempotency_key": "test-key-1"}
    payload.update(overrides)
    return payload


def test_success_increments_exactly_once(monkeypatch):
    monkeypatch.setattr(app_mod, "book", lambda *a, **k: (123, False))
    before = _outcome_count("success")

    resp = _client(monkeypatch).post("/appointments", json=_payload())

    assert resp.status_code == 201
    assert _outcome_count("success") - before == 1


def test_retry_increments_exactly_once_for_an_idempotent_replay(monkeypatch):
    monkeypatch.setattr(app_mod, "book", lambda *a, **k: (123, True))
    before = _outcome_count("retry")

    resp = _client(monkeypatch).post("/appointments", json=_payload())

    assert resp.status_code == 201
    assert _outcome_count("retry") - before == 1


def test_conflict_increments_exactly_once_for_a_taken_slot(monkeypatch):
    monkeypatch.setattr(app_mod, "book", lambda *a, **k: (None, False))
    before = _outcome_count("conflict")

    resp = _client(monkeypatch).post("/appointments", json=_payload())

    assert resp.status_code == 409
    assert _outcome_count("conflict") - before == 1


def test_conflict_increments_exactly_once_for_a_reused_idempotency_key(monkeypatch):
    """The SAME 'conflict' outcome value, via the OTHER code path
    (IdempotencyKeyConflict, not a plain taken-slot)."""
    def _raise(*a, **k):
        raise app_mod.IdempotencyKeyConflict(existing_appointment_id=99)

    monkeypatch.setattr(app_mod, "book", _raise)
    before = _outcome_count("conflict")

    resp = _client(monkeypatch).post("/appointments", json=_payload())

    assert resp.status_code == 409
    assert _outcome_count("conflict") - before == 1


def test_failure_increments_exactly_once_on_a_database_error(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(app_mod, "book", _raise)
    before = _outcome_count("failure")

    resp = _client(monkeypatch).post("/appointments", json=_payload())

    assert resp.status_code == 503
    assert _outcome_count("failure") - before == 1
