"""services/records-service/app.py::search_records (GET /records/search).

Consolidated (PR #22 reconciliation + PR #23 user_id authz): the grant check
is embedded IN the query via active_patient_ids_query (keyed on the stable
users.id and joined to an active user), so an unauthorized patient's record
body is never loaded into application memory — not just excluded from the
response — and a DB/policy error surfaces as 503, never a silent empty 200.

That's a real SQL change, so this drives the real FastAPI route against a real
in-memory SQLite database with the actual models (mirroring
tests/test_patient_access_gate.py) rather than a hand-rolled fake session.
records-service models a minimal `users` table (the User model), so grants key
on users.id with a resolvable FK.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_search")

import db as db_mod  # noqa: E402

Patient = app_mod.Patient
Record = app_mod.Record
Base = db_mod.Base
import models as models_mod  # noqa: E402

User = models_mod.User
PatientAccessGrant = models_mod.PatientAccessGrant

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
FRONTDESK = 1  # granted 1042
DRPATEL = 5    # granted 2001


def _fresh_engine_session():
    # TestClient runs the sync route in a worker thread; StaticPool +
    # check_same_thread=False pins the whole engine to the ONE connection this
    # fixture populates so the route sees the same in-memory DB.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add_all(
        [
            User(id=FRONTDESK, username="frontdesk", is_active=True, role="staff"),
            User(id=DRPATEL, username="drpatel", is_active=True, role="staff"),
            Patient(id=1042, name="Authorized Patient"),
            Patient(id=2001, name="Unrelated Patient"),
            Record(id=1, encounter_id=1, patient_id=1042, kind="note", body="Penicillin allergy noted."),
            Record(id=2, encounter_id=2, patient_id=2001, kind="note", body="Penicillin allergy noted."),
        ]
    )
    db.commit()
    return db


def _grant(db, *, user_id, patient_id):
    db.add(PatientAccessGrant(user_id=user_id, patient_id=patient_id))
    db.commit()


@pytest.fixture
def db():
    return _fresh_engine_session()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)

    def _get_db():
        yield db

    app_mod.app.dependency_overrides[app_mod.get_db] = _get_db
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def _search(client, q="penicillin", actor=str(FRONTDESK)):
    headers = {"X-Internal-Token": TEST_TOKEN}
    if actor is not None:
        headers["X-Actor-Id"] = actor
    return client.get("/records/search", params={"q": q}, headers=headers)


def test_missing_internal_token_is_rejected(client):
    resp = client.get(
        "/records/search", params={"q": "penicillin"}, headers={"X-Actor-Id": str(FRONTDESK)}
    )
    assert resp.status_code == 401


def test_authorized_patients_record_is_returned(client, db):
    _grant(db, user_id=FRONTDESK, patient_id=1042)
    resp = _search(client)
    assert resp.status_code == 200
    assert {hit["patient_id"] for hit in resp.json()} == {1042}


def test_unauthorized_patients_record_is_silently_excluded(client, db):
    _grant(db, user_id=FRONTDESK, patient_id=1042)  # granted 1042 but NOT 2001
    resp = _search(client)
    assert resp.status_code == 200
    ids = {hit["patient_id"] for hit in resp.json()}
    assert ids == {1042}
    assert 2001 not in ids


def test_actor_with_no_grants_sees_nothing(client, db):
    resp = _search(client, actor=str(DRPATEL))  # no grants for drpatel
    assert resp.status_code == 200
    assert resp.json() == []


def test_a_different_actor_with_a_different_grant_sees_only_their_own(client, db):
    _grant(db, user_id=FRONTDESK, patient_id=1042)
    _grant(db, user_id=DRPATEL, patient_id=2001)
    assert {h["patient_id"] for h in _search(client, actor=str(FRONTDESK)).json()} == {1042}
    assert {h["patient_id"] for h in _search(client, actor=str(DRPATEL)).json()} == {2001}


def test_no_matching_query_returns_empty_list_regardless_of_grants(client, db):
    _grant(db, user_id=FRONTDESK, patient_id=1042)
    resp = _search(client, q="nonexistent-term-xyz")
    assert resp.status_code == 200
    assert resp.json() == []


def test_database_failure_returns_503(client, db, monkeypatch):
    def _boom(*_a, **_kw):
        raise SQLAlchemyError("simulated connection drop")

    monkeypatch.setattr(db, "execute", _boom)
    assert _search(client).status_code == 503
