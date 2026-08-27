"""w8-planner-2 — ROI authorization gate + 45 CFR 164.528 accounting of
disclosures (services/roi-service/app.py, migration 029).

Closes two of DEBT D12's three legs: fulfill_roi_request now REQUIRES a
signed-authorization reference before releasing PHI (164.508), and every
fulfillment writes a disclosure row a real accounting can be built from
(164.528) — directly answering docs/handover/auditor-questionnaire.md's Q7
("produce an accounting of disclosures ... including to whom and under what
authorization"). 164.522 (restriction honoring) remains explicitly out of
scope — not tested here as fixed, because it isn't.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/roi-service/app.py", "roi_authorization_app")

TOKEN = "test-internal-token-for-roi-well-over-32-characters"
PATIENT_ID = 1042


def _headers():
    return {"X-Internal-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    app_mod.Patient.metadata.create_all(engine)
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

    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def _create_request(client, recipient="Dr. Chen, Riverbend East"):
    resp = client.post(
        "/roi/requests",
        json={
            "patient_id": PATIENT_ID,
            "requested_by": "front-desk-1",
            "recipient": recipient,
            "recipient_type": "provider",
            "purpose": "continuity of care",
        },
        headers=_headers(),
    )
    assert resp.status_code == 201
    return resp.json()["id"]


AUTH = {
    "authorization_reference": "signed-form-2026-0042",
    "authorization_signed_at": "2026-08-20T14:30:00Z",
    "authorization_signed_by": "Maria Gonzalez",
}


# --- 164.508: no authorization, no release -----------------------------------


def test_fulfill_without_any_body_is_rejected(client):
    request_id = _create_request(client)

    resp = client.post(f"/roi/requests/{request_id}/fulfill", headers=_headers())

    assert resp.status_code == 422


def test_fulfill_with_an_empty_authorization_reference_is_rejected(client):
    request_id = _create_request(client)
    bad = {**AUTH, "authorization_reference": ""}

    resp = client.post(f"/roi/requests/{request_id}/fulfill", json=bad, headers=_headers())

    assert resp.status_code == 422


def test_a_rejected_fulfillment_leaves_the_request_pending_and_writes_no_disclosure(client):
    request_id = _create_request(client)

    client.post(f"/roi/requests/{request_id}/fulfill", headers=_headers())  # 422, no body

    listed = client.get("/roi/requests", params={"patient_id": PATIENT_ID}, headers=_headers())
    assert listed.json()[0]["status"] == "pending"

    accounting = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers())
    assert accounting.json()["disclosures"] == []


def test_fulfill_with_a_complete_authorization_succeeds(client):
    request_id = _create_request(client)

    resp = client.post(f"/roi/requests/{request_id}/fulfill", json=AUTH, headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fulfilled"
    assert body["disclosure_id"] is not None


# --- 164.528: a real accounting of disclosures --------------------------------


def test_the_accounting_entry_names_who_when_why_and_under_what_authorization(client):
    request_id = _create_request(client, recipient="Dr. Chen, Riverbend East")
    client.post(f"/roi/requests/{request_id}/fulfill", json=AUTH, headers=_headers())

    resp = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers())

    assert resp.status_code == 200
    [entry] = resp.json()["disclosures"]
    assert entry["disclosed_to"] == "Dr. Chen, Riverbend East"
    assert entry["purpose"] == "continuity of care"
    assert entry["authorization_reference"] == "signed-form-2026-0042"
    assert entry["disclosed_at"] is not None


def test_a_patient_with_no_disclosures_gets_an_empty_accounting_not_an_error(client):
    resp = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers())

    assert resp.status_code == 200
    assert resp.json() == {"patient_id": PATIENT_ID, "disclosures": []}


def test_fulfill_can_override_the_requests_own_purpose_for_this_specific_disclosure(client):
    request_id = _create_request(client)
    override = {**AUTH, "purpose": "attorney-requested litigation hold"}

    client.post(f"/roi/requests/{request_id}/fulfill", json=override, headers=_headers())

    [entry] = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers()).json()["disclosures"]
    assert entry["purpose"] == "attorney-requested litigation hold"


def test_the_disclosure_keeps_its_own_data_even_if_the_request_is_edited_later(client):
    # The whole reason disclosures carries its own authorization_reference/
    # purpose instead of only a roi_request_id FK (see migration 029's own
    # comment): a 164.528 accounting must describe what was true AT THE TIME
    # of disclosure, not whatever the request row says afterward.
    request_id = _create_request(client)
    client.post(f"/roi/requests/{request_id}/fulfill", json=AUTH, headers=_headers())

    # Simulate the request row being edited after the fact (e.g. a
    # data-correction pass) — directly, bypassing the API, the same way a
    # future admin tool might.
    db_gen = app_mod.app.dependency_overrides[app_mod.get_db]()
    db = next(db_gen)
    try:
        req = db.get(app_mod.RoiRequest, request_id)
        req.purpose = "corrected purpose, entered after the fact"
        req.authorization_reference = "a-different-reference-entirely"
        db.commit()
    finally:
        db_gen.close()

    entry = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers()).json()["disclosures"][0]
    assert entry["purpose"] == "continuity of care"
    assert entry["authorization_reference"] == "signed-form-2026-0042"


# --- unaffected by this change: token guard, unknown request -----------------


def test_fulfilling_an_unknown_request_is_still_a_404_before_authorization_is_even_checked(client):
    resp = client.post("/roi/requests/999999/fulfill", json=AUTH, headers=_headers())

    assert resp.status_code == 404


def test_no_internal_token_is_still_refused_on_the_new_accounting_route(client):
    resp = client.get(f"/roi/patients/{PATIENT_ID}/accounting")

    assert resp.status_code == 401
