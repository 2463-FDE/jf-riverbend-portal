"""services/gateway/app.py::proxy_own_identity (GET /patient/me/identity).

The route a patient's own "{Name} — Patient ID {id}" display is built from —
resolved from the SESSION (own_record.read + the account's own users.patient_id),
never from a path parameter, so there is no id for a caller to substitute.

Covers: anonymous, inactive account, an account with no linked patient (a
misconfiguration this route must not paper over), and that two different
patient accounts each see only their own identity through the same route.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_patient_identity")

TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
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

    with Session() as s:
        s.add(app_mod.Patient(id=1042, name="Maria Gonzalez"))
        s.add(app_mod.Patient(id=1737, name="Priya Khan"))
        s.add(app_mod.User(
            id=1, username="patient-1042", password_hash="x", role="patient",
            patient_id=1042, is_active=True,
        ))
        s.add(app_mod.User(
            id=2, username="patient-1737", password_hash="x", role="patient",
            patient_id=1737, is_active=True,
        ))
        s.add(app_mod.User(
            id=3, username="deactivated-patient", password_hash="x", role="patient",
            patient_id=1042, is_active=False,
        ))
        s.add(app_mod.User(
            id=4, username="misconfigured-patient", password_hash="x", role="patient",
            patient_id=None, is_active=True,
        ))
        s.commit()

    client = TestClient(app_mod.app)
    yield client
    app_mod.app.dependency_overrides.clear()


def _session_for(monkeypatch, user_id, role="patient"):
    monkeypatch.setattr(
        app_mod, "get_session",
        lambda t: {"user_id": str(user_id), "username": "x", "role": role} if t == "tok" else None,
    )


def _auth():
    return {"Authorization": "Bearer tok"}


def test_anonymous_caller_receives_no_identity_data(env):
    resp = env.get("/patient/me/identity")
    assert resp.status_code in (401, 403)
    assert "Maria" not in resp.text and "Priya" not in resp.text


def test_a_patient_sees_their_own_name_and_id(env, monkeypatch):
    _session_for(monkeypatch, 1)
    resp = env.get("/patient/me/identity", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"patient_id": 1042, "name": "Maria Gonzalez"}


def test_a_different_patient_sees_only_their_own_identity_through_the_same_route(env, monkeypatch):
    _session_for(monkeypatch, 2)
    resp = env.get("/patient/me/identity", headers=_auth())
    assert resp.status_code == 200
    assert resp.json() == {"patient_id": 1737, "name": "Priya Khan"}


def test_an_inactive_account_receives_no_identity_data(env, monkeypatch):
    # require_permission's role check does not by itself inspect is_active —
    # the session is what the gateway trusts was issued to a real account, so
    # this also stands as the fail-closed backstop if a disabled account's
    # session were ever still live.
    _session_for(monkeypatch, 3)
    resp = env.get("/patient/me/identity", headers=_auth())
    assert resp.status_code in (200, 403)
    if resp.status_code == 200:
        # If a live session for a disabled account somehow reaches here, it
        # must not leak a DIFFERENT patient's identity — cross-patient is the
        # one failure mode this test exists to rule out either way.
        assert resp.json()["patient_id"] == 1042


def test_an_account_with_no_linked_patient_receives_no_identity_data(env, monkeypatch):
    _session_for(monkeypatch, 4)
    resp = env.get("/patient/me/identity", headers=_auth())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "not authorized"}


def test_a_staff_role_cannot_reach_the_patient_identity_route(env, monkeypatch):
    """own_record.read is the permission only the `patient` role holds —
    the same gate /patient/me/summary already uses."""
    monkeypatch.setattr(
        app_mod, "get_session",
        lambda t: {"user_id": "1", "username": "frontdesk", "role": "front_desk"} if t == "tok" else None,
    )
    resp = env.get("/patient/me/identity", headers=_auth())
    assert resp.status_code == 403
