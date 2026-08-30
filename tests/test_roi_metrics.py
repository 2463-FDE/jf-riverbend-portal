"""W10 Final Stage 6 sub-slice 2 — services/roi-service/app.py's
ROI_FULFILLMENT_OUTCOMES counter increments exactly once per business
outcome (success, rejected, retry — both its code paths — and failure).
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module
from libs.metrics.business import ROI_FULFILLMENT_OUTCOMES

app_mod = load_module("services/roi-service/app.py", "roi_metrics_app")

TOKEN = "test-internal-token-for-roi-metrics-well-over-32-chars"
PATIENT_ID = 1042
RECIPIENT = "Dr. Chen, Riverbend East"


def _outcome_count(outcome):
    return ROI_FULFILLMENT_OUTCOMES.labels(outcome=outcome)._value.get()


def _headers():
    return {"X-Internal-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    app_mod.Patient.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX disclosures_roi_request_id_unique "
            "ON disclosures (roi_request_id) WHERE roi_request_id IS NOT NULL"
        )
    Session = sessionmaker(bind=engine)

    def fake_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_db
    with Session() as s:
        s.add(app_mod.Patient(id=PATIENT_ID, name="Maria Gonzalez"))
        s.commit()

    yield TestClient(app_mod.app), Session
    app_mod.app.dependency_overrides.clear()


def _create_request(client):
    resp = client.post(
        "/roi/requests",
        json={
            "patient_id": PATIENT_ID, "requested_by": "front-desk-1", "recipient": RECIPIENT,
            "recipient_type": "provider", "purpose": "continuity of care",
        },
        headers=_headers(),
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _valid_authorization(client):
    resp = client.post(
        "/roi/authorizations",
        json={
            "patient_id": PATIENT_ID, "recipient": RECIPIENT, "purpose": "continuity of care",
            "signature_evidence_reference": "signed-form-2026-0042", "signed_by": "Maria Gonzalez",
            "signed_at": "2026-08-20T14:30:00Z",
        },
        headers=_headers(),
    )
    assert resp.status_code == 201
    auth_id = resp.json()["id"]
    review = client.post(
        f"/roi/authorizations/{auth_id}/review",
        json={"decision": "valid", "reviewed_by": "supervisor-1"},
        headers=_headers(),
    )
    assert review.status_code == 200
    return auth_id


def test_success_increments_exactly_once(client):
    client_obj, _ = client
    request_id = _create_request(client_obj)
    auth_id = _valid_authorization(client_obj)
    before = _outcome_count("success")

    resp = client_obj.post(f"/roi/requests/{request_id}/fulfill", json={"authorization_id": auth_id},
                           headers=_headers())

    assert resp.status_code == 200
    assert _outcome_count("success") - before == 1


def test_rejected_increments_exactly_once_for_an_invalid_authorization(client):
    client_obj, _ = client
    request_id = _create_request(client_obj)
    # An authorization that exists but was never reviewed 'valid'.
    resp = client_obj.post(
        "/roi/authorizations",
        json={
            "patient_id": PATIENT_ID, "recipient": RECIPIENT, "purpose": "continuity of care",
            "signature_evidence_reference": "unsigned-form", "signed_by": "Maria Gonzalez",
            "signed_at": "2026-08-20T14:30:00Z",
        },
        headers=_headers(),
    )
    assert resp.status_code == 201
    unreviewed_auth_id = resp.json()["id"]
    before = _outcome_count("rejected")

    fulfill = client_obj.post(f"/roi/requests/{request_id}/fulfill", json={"authorization_id": unreviewed_auth_id},
                              headers=_headers())

    assert fulfill.status_code == 422
    assert _outcome_count("rejected") - before == 1


def test_retry_increments_exactly_once_for_an_already_fulfilled_request(client):
    client_obj, _ = client
    request_id = _create_request(client_obj)
    auth_id = _valid_authorization(client_obj)
    first = client_obj.post(f"/roi/requests/{request_id}/fulfill", json={"authorization_id": auth_id},
                            headers=_headers())
    assert first.status_code == 200
    before = _outcome_count("retry")

    retry = client_obj.post(f"/roi/requests/{request_id}/fulfill", json={"authorization_id": auth_id},
                            headers=_headers())

    assert retry.status_code == 409
    assert _outcome_count("retry") - before == 1


def test_retry_increments_exactly_once_for_the_integrity_error_backstop(client):
    """The SAME 'retry' outcome value, via the OTHER code path (migration
    035's unique-index backstop, not the ordinary status check)."""
    client_obj, Session = client
    request_id = _create_request(client_obj)
    auth_id = _valid_authorization(client_obj)
    with Session() as s:
        s.add(app_mod.Disclosure(patient_id=PATIENT_ID, roi_request_id=request_id, disclosed_to=RECIPIENT))
        s.commit()
    before = _outcome_count("retry")

    resp = client_obj.post(f"/roi/requests/{request_id}/fulfill", json={"authorization_id": auth_id},
                           headers=_headers())

    assert resp.status_code == 409
    assert _outcome_count("retry") - before == 1


def test_failure_increments_exactly_once_on_a_database_error(client, monkeypatch):
    client_obj, _ = client
    request_id = _create_request(client_obj)
    auth_id = _valid_authorization(client_obj)
    monkeypatch.setattr(
        app_mod, "_active_restriction",
        lambda *a, **k: (_ for _ in ()).throw(SQLAlchemyError("boom")),
    )
    before = _outcome_count("failure")

    resp = client_obj.post(f"/roi/requests/{request_id}/fulfill", json={"authorization_id": auth_id},
                           headers=_headers())

    assert resp.status_code == 503
    assert _outcome_count("failure") - before == 1
