"""Stage 3 — services/records-service/app.py::get_patient_view wiring.

Drives the real FastAPI route with a fake DB session (dependency override)
and a fake repository (monkeypatched in place of SqlChartRepository), so
this runs with no Postgres — mirroring tests/test_intake_endpoint.py's
direct-function/fake-session style. Confirms: the real StaffAccessGate
denies a missing actor (403) and allows a present one (200), an invalid
purpose is rejected before authorization runs, and a real audit_logs row is
written on BOTH outcomes.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_patient_view")

from libs.patient_view_agent.contracts import ChartResult, EncounterRow  # noqa: E402

created_sessions = []


class FakeSession:
    def __init__(self):
        self.added = []
        self.commit_count = 0

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        pass


def _fake_get_db():
    session = FakeSession()
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
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def test_missing_actor_header_is_denied_and_audited(client):
    resp = client.get("/patients/1042/view")

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "unknown_actor"

    assert len(created_sessions) == 1
    assert created_sessions[0].commit_count == 1
    audit = created_sessions[0].added[0]
    assert audit.actor == "unknown"
    assert "outcome=denied" in audit.message
    assert "patient_id=1042" in audit.message


def test_authenticated_actor_gets_completed_view_and_is_audited(client):
    resp = client.get("/patients/1042/view", headers={"X-Actor-Id": "frontdesk"})

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
    resp = client.get("/patients/1042/view", headers={"X-Actor-Id": "billing-clerk"})
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "completed"


def test_invalid_purpose_is_rejected_before_authorization(client):
    resp = client.get(
        "/patients/1042/view", params={"purpose": "not-a-real-purpose"}, headers={"X-Actor-Id": "frontdesk"}
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
