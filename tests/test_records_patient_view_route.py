"""Stage 3 — services/records-service/app.py::get_patient_view wiring.

Drives the real FastAPI route with a fake DB session (dependency override)
and a fake repository (monkeypatched in place of SqlChartRepository), so
this runs with no Postgres — mirroring tests/test_intake_endpoint.py's
direct-function/fake-session style. Confirms: the internal-token check
(review fix, round 2026-08-05) rejects a direct caller before StaffAccessGate
ever runs, the real StaffAccessGate denies a missing actor (403) and allows a
present one (200) once that check passes, an invalid purpose is rejected
before authorization runs, a real audit_logs row is written on BOTH
StaffAccessGate outcomes but NEVER on an internal-token rejection, and
(round-15 review, 2026-08-06) a nonexistent patient_id is rejected with 404
before authorization or any chart read runs, with no audit_logs row either.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_patient_view")

from libs.patient_view_agent.contracts import ChartResult, EncounterRow  # noqa: E402

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def _internal_header():
    return {"X-Internal-Token": TEST_TOKEN}


created_sessions = []


class FakeSession:
    def __init__(self, *, fail_commit=False, existing_patient_ids=frozenset({1042})):
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0
        self._fail_commit = fail_commit
        self._existing_patient_ids = existing_patient_ids

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        if self._fail_commit:
            raise SQLAlchemyError("simulated audit_logs write failure")
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def get(self, _model, pk):
        # Round-15 review: get_patient_view now checks patient existence via
        # db.get(Patient, patient_id) before authorizing/reading. Every
        # existing test in this file uses patient_id=1042 and expects it to
        # exist, hence the default.
        return object() if pk in self._existing_patient_ids else None


def _fake_get_db():
    session = FakeSession()
    created_sessions.append(session)
    yield session


def _fake_get_db_failing_commit():
    session = FakeSession(fail_commit=True)
    created_sessions.append(session)
    yield session


def _fake_get_db_missing_patient():
    session = FakeSession(existing_patient_ids=frozenset())
    created_sessions.append(session)
    yield session


class FakeChartRepository:
    """Stands in for SqlChartRepository: one encounter, no records — enough
    for run_patient_view to reach a COMPLETED outcome with real evidence."""

    def __init__(self, db):
        self.db = db

    def load_chart(self, patient_id, *, correlation_id=""):
        return ChartResult(
            patient_id=patient_id,
            encounters=[
                EncounterRow(
                    id=1, patient_id=patient_id, encounter_type="office_visit", provider="Dr. Patel", status="finished"
                )
            ],
            records=[],
            reads=2,
        )


@pytest.fixture
def client(monkeypatch):
    created_sessions.clear()
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


# --- internal-token check: the review fix (round, 2026-08-05) --------------


def test_direct_caller_with_spoofed_actor_and_no_token_is_rejected(client):
    # This is exactly the bypass the review flagged: a caller hitting this
    # service directly (as it would if reached via records-service's
    # published host port) with a made-up X-Actor-Id and no proof it came
    # through the gateway.
    resp = client.get("/patients/1042/view", headers={"X-Actor-Id": "attacker"})

    assert resp.status_code == 401
    # No audit row under the spoofed actor name — rejected before StaffAccessGate
    # (and therefore before any _write_audit call) ever runs.
    assert created_sessions[0].added == []


def test_wrong_token_with_valid_looking_actor_is_rejected(client):
    resp = client.get(
        "/patients/1042/view", headers={"X-Actor-Id": "frontdesk", "X-Internal-Token": "not-the-real-token"}
    )
    assert resp.status_code == 401
    assert created_sessions[0].added == []


def test_unconfigured_token_fails_closed_even_with_matching_empty_values(client, monkeypatch):
    # The bug this guards against: if INTERNAL_SERVICE_TOKEN is unset on both
    # services, an empty configured value must NOT compare equal to an empty
    # header — that would silently reopen the exact bypass being fixed.
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "")
    resp = client.get("/patients/1042/view", headers={"X-Actor-Id": "frontdesk", "X-Internal-Token": ""})
    assert resp.status_code == 401
    assert created_sessions[0].added == []


def test_short_placeholder_token_is_rejected_even_when_it_matches_exactly(client, monkeypatch):
    # Review round 2: .env.example used to ship INTERNAL_SERVICE_TOKEN=changeme
    # — a valid-looking placeholder a real deployment could ship unmodified.
    # A short, human-typed value must fail closed even if both sides somehow
    # agree on it (e.g. an operator typed "changeme" on both services).
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "changeme")
    resp = client.get("/patients/1042/view", headers={"X-Actor-Id": "frontdesk", "X-Internal-Token": "changeme"})
    assert resp.status_code == 401
    assert created_sessions[0].added == []


# --- StaffAccessGate, once the internal-token check passes -----------------


def test_missing_actor_header_is_denied_and_audited(client):
    resp = client.get("/patients/1042/view", headers=_internal_header())

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "unknown_actor"

    assert len(created_sessions) == 1
    assert created_sessions[0].commit_count == 1
    audit = created_sessions[0].added[0]
    assert audit.actor == "unknown"
    assert "outcome=denied" in audit.message
    assert "patient_id=1042" in audit.message


def test_authenticated_actor_gets_completed_view_and_is_audited(client):
    resp = client.get("/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "frontdesk"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "completed"
    assert body["patient_id"] == 1042
    assert body["evidence_ids"]

    audit = created_sessions[0].added[0]
    assert audit.actor == "frontdesk"
    assert "outcome=completed" in audit.message
    assert "patient_id=1042" in audit.message


def test_a_different_actor_can_view_the_same_patient(client):
    # Demonstrates the gate is authenticated-staff, not patient-specific:
    # an unrelated actor is not denied for the same patient_id.
    resp = client.get("/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "billing-clerk"})
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "completed"


def test_invalid_purpose_is_rejected_before_authorization(client):
    resp = client.get(
        "/patients/1042/view",
        params={"purpose": "not-a-real-purpose"},
        headers={**_internal_header(), "X-Actor-Id": "frontdesk"},
    )
    assert resp.status_code == 400
    # get_db is a FastAPI dependency, so a session is still created, but
    # rejected before authorize() ever runs — no audit row for this request.
    assert created_sessions[0].added == []


def test_legacy_records_endpoint_is_unaffected(client):
    # This route's presence must not change the pre-existing, documented IDOR
    # behavior of the sibling endpoint below it in app.py.
    import inspect

    assert "no ownership / authorization check" in inspect.getsource(app_mod.get_patient_records)


# --- audit-write-must-not-fail-open: the review fix (round 2, 2026-08-05) --


def test_allowed_view_does_not_return_chart_data_if_audit_write_fails(monkeypatch):
    # This is exactly what the review flagged: a prior version returned 200
    # with real chart data even though the access was never durably recorded.
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_failing_commit
    try:
        resp = TestClient(app_mod.app).get(
            "/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code != 200
    assert resp.status_code == 503
    assert "patient_id" not in resp.json()
    assert "evidence_ids" not in resp.json()


def test_denial_also_fails_closed_if_audit_write_fails(monkeypatch):
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_failing_commit
    try:
        resp = TestClient(app_mod.app).get("/patients/1042/view", headers=_internal_header())
    finally:
        app_mod.app.dependency_overrides.clear()

    # Even a denial must be durably recordable, or this now surfaces as a
    # 503 rather than silently confirming/denying with no trace.
    assert resp.status_code == 503


# --- patient existence check: round-15 review (2026-08-06) -----------------
#
# SqlChartRepository's first read only loads encounters for the requested
# id — an unknown id used to come back as an empty, evidence-free chart
# instead of a 404, so a typo'd or stale patient_id looked identical to a
# real patient with no records. get_patient_view now checks existence via
# db.get(Patient, patient_id) before authorization or any chart read.


def test_nonexistent_patient_id_returns_404_and_writes_no_audit_row(monkeypatch):
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_missing_patient
    try:
        resp = TestClient(app_mod.app).get(
            "/patients/999999/view", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 404
    # Not-found, not a chart access — no audit_logs row, same reasoning as
    # the internal-token rejection path above.
    assert created_sessions[-1].added == []
    assert created_sessions[-1].commit_count == 0


def test_nonexistent_patient_id_is_404_even_for_an_actor_who_would_be_denied(monkeypatch):
    # The existence check runs before StaffAccessGate too — a request that
    # would otherwise be denied (missing X-Actor-Id) must still surface as
    # "patient not found," not fall through to a 403 that would confirm the
    # patient exists.
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_missing_patient
    try:
        resp = TestClient(app_mod.app).get("/patients/999999/view", headers=_internal_header())
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert created_sessions[-1].added == []
