"""services/records-service/app.py::search_records (GET /records/search).

Codex review (2026-08-07, PR #22 round 4 — medium): the grant check used to
be fetch-then-filter (load every matching record's body into Python, then
call authorized_patient_ids to decide which to keep). Now the grant check is
a correlated EXISTS subquery in the SAME statement as the body scan, so an
unauthorized patient's record body is never loaded into application memory
at all — not just excluded from the response.

That's a real SQL query change (a correlated subquery referencing the outer
Record.patient_id), not a Python branch — a hand-rolled fake session can't
meaningfully prove a query like that is correct, so this drives the real
FastAPI route against a real in-memory SQLite database with the actual
models, mirroring tests/test_patient_access_gate.py's approach rather than
the fake-session style used elsewhere in this directory. `patient_access_
grants.username` FKs to `users.username`, a table records-service doesn't
model (it doesn't own that table) — a bare stub `users` table is registered
on the shared metadata purely so `create_all()` can resolve the FK target;
SQLite does not enforce foreign keys by default, so this is DDL-only.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Table, Text, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_search")

# app.py's own `from db import get_db` already populated sys.modules["db"]
# as a side effect of loading app_mod above — a plain import here returns
# that SAME cached module (see tests/test_patient_access_gate.py's
# identical reasoning), guaranteeing this file builds rows with the exact
# Base/metadata app_mod queries against.
import db as db_mod  # noqa: E402

Patient = app_mod.Patient
Record = app_mod.Record
PatientAccessGrant = app_mod.PatientAccessGrant
Base = db_mod.Base

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def _fresh_engine_session():
    # `db.Base` is shared across every records-service test file in this
    # pytest session (conftest.load_module's plain-import side effect —
    # see the import comment above), so `Base.metadata` may already have a
    # "users" stub registered by a DIFFERENT test file's own copy of this
    # same pattern (e.g. test_patient_access_gate.py) by the time this
    # runs. Check the metadata directly rather than a per-file flag, which
    # only knows about ITS OWN registration and would double-register (and
    # raise) depending on test execution order.
    if "users" not in Base.metadata.tables:
        Table("users", Base.metadata, Column("username", Text, primary_key=True))

    # FastAPI's TestClient runs the (sync) route in a worker thread, unlike
    # this fixture's own thread — check_same_thread=False allows that. Just
    # as important: plain "sqlite:///:memory:" hands out a FRESH, isolated
    # in-memory database per connection checkout, so the route's own
    # checkout would see an empty, table-less database unless StaticPool
    # pins the whole engine to the ONE connection this fixture populates.
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    users_table = Base.metadata.tables["users"]
    for username in ("frontdesk", "drpatel"):
        db.execute(users_table.insert().values(username=username))
    db.add(Patient(id=1042, name="Authorized Patient"))
    db.add(Patient(id=2001, name="Unrelated Patient"))
    db.add(Record(id=1, encounter_id=1, patient_id=1042, kind="note", body="Penicillin allergy noted."))
    db.add(Record(id=2, encounter_id=2, patient_id=2001, kind="note", body="Penicillin allergy noted."))
    db.commit()
    return db


def _grant(db, *, username, patient_id):
    db.add(PatientAccessGrant(username=username, patient_id=patient_id))
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


def _search(client, q="penicillin", actor="frontdesk"):
    headers = {"X-Internal-Token": TEST_TOKEN}
    if actor is not None:
        headers["X-Actor-Id"] = actor
    return client.get("/records/search", params={"q": q}, headers=headers)


def test_missing_internal_token_is_rejected(client):
    resp = client.get("/records/search", params={"q": "penicillin"}, headers={"X-Actor-Id": "frontdesk"})
    assert resp.status_code == 401


def test_authorized_patients_record_is_returned(client, db):
    _grant(db, username="frontdesk", patient_id=1042)

    resp = _search(client, actor="frontdesk")

    assert resp.status_code == 200
    ids = {hit["patient_id"] for hit in resp.json()}
    assert ids == {1042}


def test_unauthorized_patients_record_is_silently_excluded(client, db):
    # frontdesk is granted 1042 but NOT 2001 — both have a matching record,
    # but only 1042's may appear.
    _grant(db, username="frontdesk", patient_id=1042)

    resp = _search(client, actor="frontdesk")

    assert resp.status_code == 200
    ids = {hit["patient_id"] for hit in resp.json()}
    assert ids == {1042}
    assert 2001 not in ids


def test_actor_with_no_grants_sees_nothing(client, db):
    resp = _search(client, actor="drpatel")  # no grants for drpatel at all

    assert resp.status_code == 200
    assert resp.json() == []


def test_a_different_actor_with_a_different_grant_sees_only_their_own(client, db):
    _grant(db, username="frontdesk", patient_id=1042)
    _grant(db, username="drpatel", patient_id=2001)

    resp_frontdesk = _search(client, actor="frontdesk")
    resp_drpatel = _search(client, actor="drpatel")

    assert {h["patient_id"] for h in resp_frontdesk.json()} == {1042}
    assert {h["patient_id"] for h in resp_drpatel.json()} == {2001}


def test_no_matching_query_returns_empty_list_regardless_of_grants(client, db):
    _grant(db, username="frontdesk", patient_id=1042)

    resp = _search(client, q="nonexistent-term-xyz", actor="frontdesk")

    assert resp.status_code == 200
    assert resp.json() == []


def test_database_failure_returns_503(client, db, monkeypatch):
    def _boom(*_a, **_kw):
        raise SQLAlchemyError("simulated connection drop")

    monkeypatch.setattr(db, "execute", _boom)

    resp = _search(client, actor="frontdesk")

    assert resp.status_code == 503
