"""Week 4 catch-up — services/records-service/app.py::get_patient and
get_patient_records, the ACTUAL RIV-201 IDOR (DEBT D11), not just the
additive /view route tests/test_records_patient_view_route.py already
covers.

Drives the real FastAPI routes with a fake DB session, mirroring that
file's style: `.execute()` stands in for SqlPatientAccessGate's grant
lookup (the only db.execute() call in either route's authorization path;
the chart/demographics reads themselves use `.get()`/`.execute()` on
Patient/Encounter/Record separately and are exercised via the
`existing_patient_ids`/`chart_rows` fakes below).

Covers the required security-boundary matrix for this stage:
  - an authorized actor can read the patient they have a grant for
  - an unrelated/unauthorized actor is denied
  - a caller without a valid internal token cannot spoof the actor identity
    and read a chart directly (records-service's port is published to the
    host — see docker-compose.yml)
  - authorization runs before any patient lookup: a denied actor gets the
    identical 403 whether patient_id exists or not (no existence oracle)
  - a grant-lookup failure denies closed, not open
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_authorization")


def _fake_patient(patient_id: int):
    # A minimal stand-in with exactly the attributes PatientDetail.model_validate
    # (from_attributes=True) needs — everything else defaults to None, which
    # every other field on that schema accepts.
    import types

    return types.SimpleNamespace(
        id=patient_id,
        mrn=None,
        name="Test Patient",
        first_name=None,
        last_name=None,
        dob=None,
        ssn=None,
        gender=None,
        address=None,
        city=None,
        state=None,
        zip_code=None,
        phone=None,
        email=None,
        notes=None,
        created_via=None,
        created_at=None,
    )

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def _internal_header():
    return {"X-Internal-Token": TEST_TOKEN}


created_sessions = []



class _FakeActorRow:
    """The (role, is_active) row records-service now reads to enforce the
    signed permission matrix at the data boundary and to revalidate the actor
    on every request. `staff` is the legacy full-permission role every real
    account still carries, so it preserves what these tests were written to
    exercise: grant-based authorization, not role-based denial."""

    def __init__(self, role="staff", is_active=True):
        self.role = role
        self.is_active = is_active


class _FakeActorResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row

class _FakeGrantResult:
    def __init__(self, found: bool):
        self._found = found

    def first(self):
        return (1,) if self._found else None


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    """Same shape as test_records_patient_view_route.py's FakeSession, plus
    enough `.execute()` handling for get_patient_records' own encounter/
    record queries (both go through `.scalars().all()`, distinct from the
    grant lookup's `.first()`)."""

    def __init__(
        self,
        *,
        existing_patient_ids=frozenset({1042}),
        grant_exists=True,
        fail_execute=False,
    ):
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0
        self._existing_patient_ids = existing_patient_ids
        self._grant_exists = grant_exists
        self._fail_execute = fail_execute

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def get(self, _model, pk):
        return _fake_patient(pk) if pk in self._existing_patient_ids else None

    def execute(self, _stmt):
        if self._fail_execute:
            raise SQLAlchemyError("simulated db failure")
        # The grant lookup is the only .first()-shaped call in these two
        # routes' authorization path; encounter/record reads use
        # .scalars().all() and just need to return an empty chart here —
        # this file is about authorization, not chart assembly (already
        # covered by tests/test_records_flow.py-style coverage elsewhere).
        return _FakeGrantResultOrEmptyScalars(self._grant_exists)


class _FakeGrantResultOrEmptyScalars(_FakeGrantResult):
    """One object that satisfies both call shapes this fake needs to
    support (`.first()` for the grant lookup, `.scalars().all()` for the
    encounter/record queries in get_patient_records) without needing to
    parse the real SQL to tell them apart."""

    def scalars(self):
        return _FakeScalars([])


def _fake_get_db():
    session = FakeSession()
    created_sessions.append(session)
    yield session


def _fake_get_db_no_grant():
    session = FakeSession(grant_exists=False)
    created_sessions.append(session)
    yield session


def _fake_get_db_execute_failing():
    session = FakeSession(fail_execute=True)
    created_sessions.append(session)
    yield session


@pytest.fixture
def client(monkeypatch):
    created_sessions.clear()
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


ROUTES = ["/patients/1042", "/patients/1042/records"]


# --- authorized access works -----------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_authorized_actor_can_read_the_patient_they_have_a_grant_for(client, route):
    resp = client.get(route, headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"})

    assert resp.status_code == 200
    audit = created_sessions[0].added[0]
    assert audit.actor == "frontdesk"
    assert "outcome=allowed" in audit.message
    assert "patient_id=1042" in audit.message


# --- unrelated/unauthorized actor is denied ---------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_unrelated_actor_is_denied(monkeypatch, route):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_no_grant
    try:
        resp = TestClient(app_mod.app).get(route, headers={**_internal_header(), "X-Actor-Id": "2", "X-Actor-Name": "billing-clerk"})
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "not_authorized"
    audit = created_sessions[-1].added[0]
    assert audit.actor == "billing-clerk"
    assert "outcome=denied" in audit.message


# --- direct access cannot spoof the actor identity --------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_direct_caller_cannot_spoof_actor_without_a_valid_internal_token(client, route):
    # This is the actual RIV-201 bypass path: records-service's port is
    # published to the host (docker-compose.yml), so without the internal
    # token check a direct caller could hit this route with a made-up
    # X-Actor-Id and never touch the gateway/session layer at all.
    resp = client.get(route, headers={"X-Actor-Id": "attacker"})

    assert resp.status_code == 401
    assert created_sessions[0].added == []  # rejected before any grant lookup or audit write


@pytest.mark.parametrize("route", ROUTES)
def test_wrong_internal_token_is_rejected_even_with_a_known_actor(client, route):
    resp = client.get(route, headers={"X-Actor-Id": "1", "X-Actor-Name": "frontdesk", "X-Internal-Token": "not-the-real-token"})

    assert resp.status_code == 401
    assert created_sessions[0].added == []


# --- authorization runs before any patient lookup (no existence oracle) ----
#
# The two tests below are the real proof: a denied actor gets the IDENTICAL
# 403/reason whether patient_id exists (test_denied_actor_gets_the_identical
# _403_for_an_existing_patient_too, below) or doesn't (this one) — if
# authorization ran AFTER an existence check instead of before it, a caller
# with a valid token but no grant could tell the two cases apart purely from
# the status code (403 vs 404), a patient-ID enumeration oracle.


@pytest.mark.parametrize("route", ["/patients/999999", "/patients/999999/records"])
def test_denied_actor_gets_403_not_404_for_a_nonexistent_patient(monkeypatch, route):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    session = FakeSession(existing_patient_ids=frozenset(), grant_exists=False)
    created_sessions.append(session)

    def _get_db():
        yield session

    app_mod.app.dependency_overrides[app_mod.get_db] = _get_db
    try:
        resp = TestClient(app_mod.app).get(route, headers={**_internal_header(), "X-Actor-Id": "2", "X-Actor-Name": "billing-clerk"})
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "not_authorized"
    assert session.commit_count == 1  # denial was audited, not a bare 404


@pytest.mark.parametrize("route", ["/patients/1042", "/patients/1042/records"])
def test_denied_actor_gets_the_identical_403_for_an_existing_patient_too(monkeypatch, route):
    # The other half of the oracle check: an existing patient_id with a
    # denied actor must produce the SAME 403/reason as the nonexistent one
    # above, not a different status that would let a denied caller tell
    # "exists" apart from "doesn't."
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    session = FakeSession(existing_patient_ids=frozenset({1042}), grant_exists=False)
    created_sessions.append(session)

    def _get_db():
        yield session

    app_mod.app.dependency_overrides[app_mod.get_db] = _get_db
    try:
        resp = TestClient(app_mod.app).get(route, headers={**_internal_header(), "X-Actor-Id": "2", "X-Actor-Name": "billing-clerk"})
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "not_authorized"


# --- grant lookup failure denies closed -------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_grant_lookup_failure_denies_closed(monkeypatch, route):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_execute_failing
    try:
        resp = TestClient(app_mod.app).get(route, headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"})
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "policy_error"
