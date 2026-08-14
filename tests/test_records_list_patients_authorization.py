"""PR #23 review round 2 (2026-08-07) — services/records-service/app.py
list_patients (GET /patients, finding 2) and search_records (GET
/records/search, finding 4) are now patient-scoped: results are filtered IN
SQL to the caller's active grants, keyed on users.id, joined to an active user.
A DB/policy error is a 503, never a silently empty 200, and the patients
actually returned are audited.

Exercised against a REAL in-memory SQLite database (not a hand-rolled fake) so
the grant filter's SQL semantics are actually proven — a fake that returns
fixed rows regardless of the query cannot show that unauthorized rows are
excluded.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_list_patients")

import db as db_mod  # noqa: E402
import models as models_mod  # noqa: E402

Base = db_mod.Base
User = models_mod.User
Patient = models_mod.Patient
Record = models_mod.Record
Encounter = models_mod.Encounter
PatientAccessGrant = models_mod.PatientAccessGrant
AuditLog = models_mod.AuditLog

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
FRONTDESK = 1  # active, granted 1042 only
BILLING = 2    # active, no grants
DISABLED = 3   # inactive, granted 1043


def _seeded_session():
    # StaticPool + check_same_thread=False: TestClient runs the endpoint in a
    # threadpool, so the in-memory DB must share one connection across threads.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add_all(
        [
            User(id=FRONTDESK, username="frontdesk", is_active=True, role="staff"),
            User(id=BILLING, username="billing-clerk", is_active=True, role="staff"),
            User(id=DISABLED, username="disabled-doc", is_active=False, role="staff"),
            Patient(id=1042, name="Maria Gonzalez"),
            Patient(id=2001, name="Unrelated Patient"),
            Patient(id=1043, name="James O'Brien"),
            Record(id=1, encounter_id=1, patient_id=1042, kind="note", body="penicillin allergy noted"),
            Record(id=2, encounter_id=2, patient_id=1043, kind="note", body="penicillin discussed"),
            # grants: frontdesk -> 1042 only; disabled-doc -> 1043 (but inactive)
            PatientAccessGrant(user_id=FRONTDESK, patient_id=1042),
            PatientAccessGrant(user_id=DISABLED, patient_id=1043),
        ]
    )
    db.commit()
    return db


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    db = _seeded_session()

    def _override_get_db():
        yield db

    app_mod.app.dependency_overrides[app_mod.get_db] = _override_get_db
    yield TestClient(app_mod.app), db
    app_mod.app.dependency_overrides.clear()


def _auth(actor_id=str(FRONTDESK), actor_name="frontdesk"):
    h = {"X-Internal-Token": TEST_TOKEN}
    if actor_id is not None:
        h["X-Actor-Id"] = actor_id
    if actor_name is not None:
        h["X-Actor-Name"] = actor_name
    return h


def _audits(db):
    return db.query(AuditLog).all()


# --- internal-token boundary (unchanged) ------------------------------------


def test_direct_caller_without_internal_token_is_rejected(ctx):
    client, _ = ctx
    assert client.get("/patients").status_code == 401


def test_wrong_internal_token_is_rejected(ctx):
    client, _ = ctx
    assert client.get("/patients", headers={"X-Internal-Token": "nope"}).status_code == 401


# --- finding 2: roster is scoped to the caller's grants ---------------------


def test_roster_is_filtered_to_the_callers_active_grants(ctx):
    client, _ = ctx
    body = client.get("/patients", headers=_auth()).json()
    assert body["total"] == 1
    assert [p["id"] for p in body["items"]] == [1042]  # not 2001, not 1043


def test_actor_with_no_grants_sees_an_empty_roster(ctx):
    client, _ = ctx
    body = client.get("/patients", headers=_auth(str(BILLING), "billing-clerk")).json()
    assert body["total"] == 0 and body["items"] == []


def test_disabled_user_is_denied_the_roster_outright(ctx):
    # Behaviour tightened in PR #33: this used to return 200 with an empty
    # items list, because the grant query's is_active join happened to exclude
    # the row — fail-closed by accident rather than by design, and an empty
    # success is indistinguishable from "no patients match". The actor check now
    # denies a disabled account explicitly, which is both more correct and
    # consistent with every other route.
    client, _ = ctx
    resp = client.get("/patients", headers=_auth(str(DISABLED), "disabled-doc"))

    assert resp.status_code == 403
    assert "items" not in resp.json()  # no data leaks alongside the denial


def test_no_actor_returns_empty_and_is_audited(ctx):
    client, db = ctx
    resp = client.get("/patients", headers={"X-Internal-Token": TEST_TOKEN})
    assert resp.status_code == 200 and resp.json()["items"] == []
    assert any("no valid actor" in a.message for a in _audits(db))


def test_returned_roster_is_audited(ctx):
    client, db = ctx
    client.get("/patients", headers=_auth())
    assert any("list_patients returned 1 patient" in a.message and a.actor == "frontdesk" for a in _audits(db))


def test_roster_db_failure_is_503_not_empty(ctx, monkeypatch):
    client, db = ctx

    def _boom(*_a, **_kw):
        raise SQLAlchemyError("simulated db failure")

    monkeypatch.setattr(db, "execute", _boom)
    assert client.get("/patients", headers=_auth()).status_code == 503


# --- finding 4: record search is scoped in-SQL, 503 on failure --------------


def test_search_returns_only_records_for_authorized_patients(ctx):
    client, _ = ctx
    hits = client.get("/records/search", headers=_auth(), params={"q": "penicillin"}).json()
    # both 1042 and 1043 have a matching body, but frontdesk is only granted 1042
    assert {h["patient_id"] for h in hits} == {1042}


def test_search_with_no_actor_returns_empty(ctx):
    client, _ = ctx
    resp = client.get(
        "/records/search", headers={"X-Internal-Token": TEST_TOKEN}, params={"q": "penicillin"}
    )
    assert resp.status_code == 200 and resp.json() == []


def test_search_db_failure_is_503_not_silently_empty(ctx, monkeypatch):
    client, db = ctx

    def _boom(*_a, **_kw):
        raise SQLAlchemyError("simulated grant/store failure")

    monkeypatch.setattr(db, "execute", _boom)
    # The pre-fix bug: a grant-lookup failure returned an empty 200. Now 503.
    assert client.get("/records/search", headers=_auth(), params={"q": "x"}).status_code == 503


def test_search_results_are_audited(ctx):
    client, db = ctx
    client.get("/records/search", headers=_auth(), params={"q": "penicillin"})
    assert any("records_search returned" in a.message and a.actor == "frontdesk" for a in _audits(db))


def test_roster_requires_the_patients_read_permission(monkeypatch):
    """PR #33 review [high]: this route filtered by active grant only and never
    checked the caller's role, so an active user holding a grant could list
    patient names, DOB, gender and MRN with an unknown or downgraded role."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "records-service"))
    import roles_config
    roles_config.reload()

    # it_admin holds no patient-scoped permission at all in the signed grid.
    assert "patients.read" not in roles_config.permissions_for("it_admin")
    # ...and an unrecognised role holds nothing, fail-closed.
    assert roles_config.permissions_for("no-such-role") == set()
