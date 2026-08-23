"""w9-fixes P0 4.5 — appointment list/book/cancel must be grant-scoped, not
just role-scoped.

appointments.read/write says a role may use scheduling at all; it says
nothing about a SPECIFIC patient. Before this fix, the gateway forwarded
list/book/cancel to scheduling-service (which has no actor context of its
own) as soon as the role check passed — an actor with the appointments
permission but no grant for a given patient could still read, book, or
cancel appointments for that patient. Downstream calls are mocked (httpx),
the same way test_gateway_rbac.py already does for other proxied routes —
what is under test here is that a denial happens BEFORE any such call.
"""
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_appointment_authz")
# Appointment is not imported directly into app.py's namespace (only used by
# visit_authorization.find_authorized_appointment) — reached the same way
# other test files reach a models class app.py doesn't import by name.
models = sys.modules[app_mod.Patient.__module__]

TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
VALID = "valid-token-abc"
GRANTED_PATIENT = 1737
UNGRANTED_PATIENT = 1042
GRANTED_USER_ID = 960
GRANTED_APPT_ID = 501
UNGRANTED_APPT_ID = 502


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    app_mod.User.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def fake_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_db
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)
    monkeypatch.setattr(
        app_mod, "get_session",
        lambda t: {"user_id": str(GRANTED_USER_ID), "username": "frontdesk", "role": "front_desk"}
        if t == VALID else None,
    )

    with Session() as s:
        s.add(app_mod.User(id=GRANTED_USER_ID, username="frontdesk", password_hash="x",
                            role="front_desk", is_active=True))
        s.add(app_mod.PatientAccessGrant(user_id=GRANTED_USER_ID, patient_id=GRANTED_PATIENT))
        s.add(models.Appointment(id=GRANTED_APPT_ID, patient_id=GRANTED_PATIENT))
        s.add(models.Appointment(id=UNGRANTED_APPT_ID, patient_id=UNGRANTED_PATIENT))
        s.commit()

    yield TestClient(app_mod.app), Session
    app_mod.app.dependency_overrides.clear()


def _auth():
    return {"Authorization": f"Bearer {VALID}"}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def test_listing_an_ungranted_patients_appointments_is_denied_before_any_call(env, monkeypatch):
    api, _ = env
    called = []
    monkeypatch.setattr(app_mod.httpx, "get", lambda *a, **k: called.append(1) or _FakeResponse())

    resp = api.get(f"/appointments?patient_id={UNGRANTED_PATIENT}", headers=_auth())

    assert resp.status_code == 403
    assert called == [], "an ungranted list must never reach scheduling-service"


def test_listing_a_granted_patients_appointments_succeeds(env, monkeypatch):
    api, _ = env
    monkeypatch.setattr(app_mod.httpx, "get", lambda *a, **k: _FakeResponse(200, {"items": []}))

    resp = api.get(f"/appointments?patient_id={GRANTED_PATIENT}", headers=_auth())

    assert resp.status_code == 200


def test_booking_for_an_ungranted_patient_is_denied_before_any_mutation(env, monkeypatch):
    api, _ = env
    called = []
    monkeypatch.setattr(app_mod.httpx, "post", lambda *a, **k: called.append(1) or _FakeResponse(201))

    resp = api.post(
        "/appointments",
        json={"patient_id": UNGRANTED_PATIENT, "slot_id": 1, "idempotency_key": "k1"},
        headers=_auth(),
    )

    assert resp.status_code == 403
    assert called == [], "an ungranted booking must never reach scheduling-service"


def test_booking_for_a_granted_patient_succeeds(env, monkeypatch):
    api, _ = env
    monkeypatch.setattr(app_mod.httpx, "post", lambda *a, **k: _FakeResponse(201, {"id": 1}))

    resp = api.post(
        "/appointments",
        json={"patient_id": GRANTED_PATIENT, "slot_id": 1, "idempotency_key": "k1"},
        headers=_auth(),
    )

    assert resp.status_code == 201


def test_cancelling_an_ungranted_patients_appointment_is_denied_before_any_mutation(env, monkeypatch):
    api, _ = env
    called = []
    monkeypatch.setattr(app_mod.httpx, "post", lambda *a, **k: called.append(1) or _FakeResponse(200))

    resp = api.post(f"/appointments/{UNGRANTED_APPT_ID}/cancel", headers=_auth())

    assert resp.status_code == 403
    assert called == [], "an ungranted cancel must never reach scheduling-service"


def test_cancelling_a_granted_patients_appointment_succeeds(env, monkeypatch):
    api, _ = env
    monkeypatch.setattr(app_mod.httpx, "post", lambda *a, **k: _FakeResponse(200, {"status": "cancelled"}))

    resp = api.post(f"/appointments/{GRANTED_APPT_ID}/cancel", headers=_auth())

    assert resp.status_code == 200


def test_cancelling_a_nonexistent_appointment_is_denied_identically(env, monkeypatch):
    """No existence oracle: a made-up id must read the same as an ungranted
    real one."""
    api, _ = env
    monkeypatch.setattr(app_mod.httpx, "post", lambda *a, **k: _FakeResponse(200))

    resp = api.post("/appointments/999999/cancel", headers=_auth())

    assert resp.status_code == 403
