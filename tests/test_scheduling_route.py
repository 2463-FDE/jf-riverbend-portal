"""Route-level tests for services/scheduling-service/app.py::create_appointment
— Stage 4 (Week 5, RIV-175, migration 013).

Drives the real FastAPI route via TestClient with app_mod.book monkeypatched
directly (book.py's own behavior is covered in isolation by
tests/test_scheduling_book.py) — this file is about the route's contract:
idempotency_key is required, and book()'s (appointment_id, is_replay) tuple
maps to the right HTTP response.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/scheduling-service/app.py", "scheduling_app")


# Branch 7: this service now verifies that a call came through the gateway, so
# every route below needs the shared token. Set here rather than per-test, the
# same way the intake/records route tests handle their own guard — the token is
# transport trust, not the behaviour these tests are about.
TEST_INTERNAL_TOKEN = "t" * 32


def _client(monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)
    else:
        app_mod.settings.internal_service_token = TEST_INTERNAL_TOKEN
    client = TestClient(app_mod.app)
    client.headers.update({"X-Internal-Token": TEST_INTERNAL_TOKEN})
    return client


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


def test_slot_taken_by_a_different_booking_returns_409(monkeypatch):
    # Round-22 review (2026-08-06): this used to be 201 with
    # status="slot_taken" in the body — a losing concurrent booking looked
    # like a success to any caller (the frontend included) that only
    # checked the HTTP status.
    monkeypatch.setattr(app_mod, "book", lambda *a, **k: (None, False))

    resp = _client().post("/appointments", json=_payload())

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "slot_taken"


def test_idempotency_key_conflict_returns_409(monkeypatch):
    def _raise(*a, **k):
        raise app_mod.IdempotencyKeyConflict(existing_appointment_id=99)

    monkeypatch.setattr(app_mod, "book", _raise)

    resp = _client().post("/appointments", json=_payload())

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "idempotency_key_conflict"
    assert resp.json()["detail"]["existing_appointment_id"] == 99


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


# --- branch 7: the guard itself --------------------------------------------


def test_a_call_without_the_internal_token_is_refused(monkeypatch):
    """The whole point of the branch. Without this the tests above would pass
    just as happily against a service that checks nothing — they supply the
    token, so they can only ever prove it is accepted."""
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)

    resp = TestClient(app_mod.app).post("/appointments", json=_payload())

    assert resp.status_code == 401


def test_a_call_with_the_wrong_internal_token_is_refused(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)

    client = TestClient(app_mod.app)
    client.headers.update({"X-Internal-Token": "w" * 32})
    resp = client.post("/appointments", json=_payload())

    assert resp.status_code == 401


def test_an_unset_configured_token_fails_closed(monkeypatch):
    """A service with no token configured must refuse everything, not accept
    everything. Reading an empty secret as "no check needed" is how a
    misconfigured deploy silently loses its authentication."""
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "")

    client = TestClient(app_mod.app)
    client.headers.update({"X-Internal-Token": ""})
    resp = client.post("/appointments", json=_payload())

    assert resp.status_code == 401


def test_a_short_placeholder_token_is_not_accepted(monkeypatch):
    """"changeme" and friends. A human-typed placeholder is not a secret, and
    treating it as one is worse than having no check, because it looks
    configured."""
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "changeme")

    client = TestClient(app_mod.app)
    client.headers.update({"X-Internal-Token": "changeme"})
    resp = client.post("/appointments", json=_payload())

    assert resp.status_code == 401


def test_healthz_does_not_require_the_token(monkeypatch):
    """Deliberately unguarded: compose's healthcheck calls it, and a probe
    needing the app secret turns a token misconfiguration into an
    unexplained unhealthy container instead of a clear 401 on real traffic."""
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)

    assert TestClient(app_mod.app).get("/healthz").status_code == 200


def test_the_service_refuses_to_start_with_an_unusable_token(monkeypatch):
    """Pre-merge review of #43: _internal_token_is_configured existed and
    nothing called it, so the docstring's promise of a loud startup failure was
    not kept and this service would have booted clean, passed its healthcheck,
    and 401'd every request — the healthy-looking outage the round-13/17
    reviews fixed for gateway, intake and records.

    Compose's ${VAR:?...} catches an entirely missing value before any
    container starts. It cannot catch one that is present but unusable, which
    is exactly what this covers.
    """
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "changeme")

    with pytest.raises(RuntimeError, match="refusing to start"):
        app_mod._fail_fast_on_an_unusable_token()


def test_the_service_starts_with_a_real_token(monkeypatch):
    """The other direction, so the check cannot be 'fixed' by always raising."""
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "r" * 32)

    app_mod._fail_fast_on_an_unusable_token()   # must not raise
