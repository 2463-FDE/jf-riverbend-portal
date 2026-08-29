"""services/gateway/app.py::proxy_own_identity (GET /patient/me/identity).

The route a patient's own "{Name} — Patient ID {id}" display is built from —
resolved from the SESSION (own_record.read + the account's own users.patient_id),
never from a path parameter, so there is no id for a caller to substitute.

Covers: anonymous, inactive account, an account with no linked patient (a
misconfiguration this route must not paper over), a linked account with no
active self-grant (revoked or never issued), and that two different patient
accounts each see only their own identity through the same route.

Round-1 code review (B1) found this route resolved `patients.name` from
session -> users.patient_id alone, with no `has_active_grant` check — every
account fixtured below now carries the same self-grant activation actually
creates, except the ones deliberately built to prove denial without one.
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
        s.add(app_mod.Patient(id=1739, name="Aisha Taylor"))
        s.add(app_mod.User(
            id=5, username="grantless-patient", password_hash="x", role="patient",
            patient_id=1739, is_active=True,
        ))
        s.commit()

        # Self-grants — the same row activation creates (app.py:569). Users 1
        # and 2 are the "everything is fine" path; user 3's grant proves the
        # is_active join in has_active_grant, not just the grant's own
        # revoked_at/expires_at, is what closes B1 — the grant itself is
        # perfectly live, only the account is disabled. User 4 (no patient_id)
        # and user 5 (grantless) get none on purpose.
        s.add(app_mod.PatientAccessGrant(user_id=1, patient_id=1042))
        s.add(app_mod.PatientAccessGrant(user_id=2, patient_id=1737))
        s.add(app_mod.PatientAccessGrant(user_id=3, patient_id=1042))
        s.commit()

    client = TestClient(app_mod.app)
    yield client
    app_mod.app.dependency_overrides.clear()


def _session_for(monkeypatch, user_id, role="patient"):
    monkeypatch.setattr(
        app_mod, "get_session",
        lambda t: {"user_id": str(user_id), "username": "x", "role": role, "security_version": "0"}
        if t == "tok" else None,
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
    # W10 Stage 1: require_session itself now revalidates is_active against
    # the DB on every request and kills the session outright (401) before
    # require_permission or has_active_grant's own is_active join ever run —
    # user 3's otherwise-live grant for 1042 no longer matters, because the
    # session never gets that far.
    _session_for(monkeypatch, 3)
    resp = env.get("/patient/me/identity", headers=_auth())
    assert resp.status_code == 401


def test_a_patient_with_no_active_grant_receives_no_identity_data(env, monkeypatch):
    """A linked, active account with no self-grant at all — revoked or never
    issued, this route cannot tell which and must not care. own_record.read
    proves the caller is A patient, not that this specific grant still
    stands; that second check is what B1 found missing."""
    _session_for(monkeypatch, 5)
    resp = env.get("/patient/me/identity", headers=_auth())
    assert resp.status_code == 403
    assert "Aisha" not in resp.text


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
        lambda t: {"user_id": "1", "username": "frontdesk", "role": "front_desk", "security_version": "0"}
        if t == "tok" else None,
    )
    resp = env.get("/patient/me/identity", headers=_auth())
    assert resp.status_code == 403
