"""The portal path: generate -> clinician review -> approved-only display.

Drives the real records-service routes against a real SQLite session, so the
authorization these tests are about is the actual one — the permission lookup
and the per-(actor, patient) grant query both run.

The model is never involved: drafts are created through the merged write path
with a `fixture` label, because what is under test here is who may see which
version, not what the agent writes.
"""
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_agent_portal")
drafts = app_mod.agent_drafts
# PatientAccessGrant is not imported into app.py; reach it through the models
# module app.py itself bound, so this fixture builds rows in the same metadata.
models = sys.modules[app_mod.AgentDraftProvenance.__module__]

TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
PATIENT = 1737
OTHER_PATIENT = 1042
CLINICIAN_ID, PATIENT_USER_ID, OTHER_PATIENT_USER_ID = 900, 901, 902

V1_TEXT = "Version one, approved."
V2_TEXT = "Version two, still pending."


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    app_mod.AgentDraftProvenance.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add_all([
        app_mod.Patient(id=PATIENT, name="Demo Patient"),
        app_mod.Patient(id=OTHER_PATIENT, name="Other Patient"),
        app_mod.User(id=CLINICIAN_ID, username="drkim", role="clinician", is_active=True),
        app_mod.User(id=PATIENT_USER_ID, username="patient-1737", role="patient", is_active=True),
        app_mod.User(id=OTHER_PATIENT_USER_ID, username="patient-1042", role="patient",
                     is_active=True),
    ])
    db.flush()
    db.add_all([
        models.PatientAccessGrant(user_id=CLINICIAN_ID, patient_id=PATIENT),
        models.PatientAccessGrant(user_id=PATIENT_USER_ID, patient_id=PATIENT),
        models.PatientAccessGrant(user_id=OTHER_PATIENT_USER_ID, patient_id=OTHER_PATIENT),
    ])
    db.commit()

    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = lambda: db
    yield TestClient(app_mod.app), db
    app_mod.app.dependency_overrides.clear()
    db.close()


def _headers(user_id: int, name: str):
    return {"X-Internal-Token": TOKEN, "X-Actor-Id": str(user_id), "X-Actor-Name": name}


CLINICIAN = _headers(CLINICIAN_ID, "drkim")
PATIENT_H = _headers(PATIENT_USER_ID, "patient-1737")
OTHER_H = _headers(OTHER_PATIENT_USER_ID, "patient-1042")


def _draft(db, text, *, patient_id=PATIENT, passed=True):
    row = drafts.create_draft(
        db, patient_id=patient_id, generated_text=text, correlation_id="corr-portal-1",
        provenance_label=drafts.LABEL_FIXTURE, model_id="scripted-model-v0",
        prompt_version="summary-agent-v1",
        citations=[{"source_id": "POL-001", "source_version": "2026-08-01",
                    "citation_id": "POL-001@2026-08-01", "category": "policy"}],
    )
    drafts.record_validation(db, row, passed=passed,
                             validation_code=None if passed else "REFUSED_NO_CLAIMS")
    db.commit()
    return row


def test_a_patient_cannot_read_another_patients_agent_summary(client):
    api, db = client
    approved = _draft(db, V1_TEXT)
    drafts.decide(db, approved, approve=True, reviewed_by=CLINICIAN_ID)
    db.commit()

    denied = api.get(f"/patients/{PATIENT}/agent-summary", headers=OTHER_H)
    assert denied.status_code == 403, "a grant for another chart is not a grant for this one"
    assert V1_TEXT not in denied.text

    # The clinician-only draft route is closed to the patient it belongs to too:
    # holding own_record.read is not holding summary_review.decide.
    assert api.get(f"/patients/{PATIENT}/agent-draft", headers=PATIENT_H).status_code == 403


def test_a_pending_or_rejected_draft_is_never_displayed(client):
    api, db = client
    pending = _draft(db, "Pending text.")

    hidden = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H)
    assert hidden.status_code == 200 and hidden.json()["available"] is False
    assert "Pending text." not in hidden.text

    drafts.decide(db, pending, approve=False, reviewed_by=CLINICIAN_ID)
    db.commit()
    after = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H)
    assert after.json()["available"] is False, "a rejection is not a weaker approval"
    assert "Pending text." not in after.text

    # The clinician may read exactly what the patient may not.
    clinician_view = api.get(f"/patients/{PATIENT}/agent-draft", headers=CLINICIAN)
    assert clinician_view.status_code == 200
    assert clinician_view.json()["generated_text"] == "Pending text."


def test_clinician_approval_exposes_the_exact_stored_text(client):
    api, db = client
    row = _draft(db, V1_TEXT)

    decided = api.post(f"/agent-drafts/{row.id}/decision", json={"decision": "approved"},
                       headers=CLINICIAN)
    assert decided.status_code == 200 and decided.json()["status"] == drafts.APPROVED

    shown = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()
    assert shown["available"] is True
    assert shown["generated_text"] == V1_TEXT, "the exact stored text, never a regeneration"
    assert shown["version"] == 1
    assert shown["provenance_label"] == drafts.LABEL_FIXTURE
    assert [c["citation_id"] for c in shown["citations"]] == ["POL-001@2026-08-01"]


def test_a_pending_version_2_does_not_replace_an_approved_version_1(client):
    api, db = client
    v1 = _draft(db, V1_TEXT)
    api.post(f"/agent-drafts/{v1.id}/decision", json={"decision": "approved"}, headers=CLINICIAN)

    v2 = _draft(db, V2_TEXT)
    assert v2.version == 2

    still_v1 = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()
    assert still_v1["version"] == 1
    assert still_v1["generated_text"] == V1_TEXT
    assert V2_TEXT not in str(still_v1), "an unapproved regeneration never reaches the patient"

    api.post(f"/agent-drafts/{v2.id}/decision", json={"decision": "approved"}, headers=CLINICIAN)
    now_v2 = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()
    assert now_v2["version"] == 2 and now_v2["generated_text"] == V2_TEXT


def test_a_patient_can_request_their_own_summary_and_not_another_patients(client):
    """Asking is the demo's first move, and it must not become reading.

    This one runs the real generation path (no Bedrock configured, so it takes
    the deterministic fallback), because the point is what the RESPONSE carries
    back to the patient who asked.
    """
    api, db = client

    mine = api.post(f"/patients/{PATIENT}/agent-summary/request", headers=PATIENT_H)
    assert mine.status_code == 201
    body = mine.json()
    assert body["patient_id"] == PATIENT and body["version"] == 1
    assert body["correlation_id"], "the receipt names the trace this request started"
    assert "generated_text" not in body, "asking is not reading"
    assert "Riverbend releases laboratory results" not in mine.text, "no draft text, anywhere"

    # The draft it created is not displayable to them yet.
    assert api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()["available"] is False

    theirs = api.post(f"/patients/{OTHER_PATIENT}/agent-summary/request", headers=PATIENT_H)
    assert theirs.status_code == 403, "a patient holds a grant for exactly one chart"
    assert app_mod._latest_draft(db, OTHER_PATIENT) is None, "and nothing was generated for it"
