"""W10 Final 2 Stage 1 — gateway-side ROI trust boundary.

Before this stage, every /roi/* proxy route forwarded the caller's raw JSON
body to roi-service with only a ROLE permission check (roi.write/
disclosures.read) — no check that the calling staff member holds any
relationship to the SPECIFIC patient a request/authorization concerns, and
requested_by/reviewed_by/revoked_by were taken straight from that JSON body,
letting a caller claim to be anyone. These tests pin the fix: a real
patient_access_grants-backed check per route, actor identity always
overridden from the session, and real downstream status codes reaching the
caller instead of being flattened to 200.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_roi_authorization")

VALID_TOKEN = "valid-token-abc"
TEST_INTERNAL_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
TEST_USER_ID = 2
GRANTED_PATIENT_ID = 1042
UNGRANTED_PATIENT_ID = 999999


def _session() -> dict:
    return {"user_id": str(TEST_USER_ID), "username": "roiclerk1", "role": "roi_clerk", "security_version": "0"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session() if t == VALID_TOKEN else None)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    app_mod.User.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def fake_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_db
    with Session() as s:
        s.add(app_mod.User(id=TEST_USER_ID, username="roiclerk1", password_hash="x", role="roi_clerk", is_active=True))
        s.add(app_mod.PatientAccessGrant(user_id=TEST_USER_ID, patient_id=GRANTED_PATIENT_ID))
        s.add(app_mod.RoiRequest(id=1, patient_id=GRANTED_PATIENT_ID))
        s.add(app_mod.RoiRequest(id=2, patient_id=UNGRANTED_PATIENT_ID))
        s.add(app_mod.RoiAuthorization(id=1, patient_id=GRANTED_PATIENT_ID))
        s.add(app_mod.RoiAuthorization(id=2, patient_id=UNGRANTED_PATIENT_ID))
        s.commit()

    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def _auth():
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _stub_downstream(monkeypatch, payload=None, status_code=200):
    captured = {}

    def fake_get(url, **kwargs):
        captured["get"] = kwargs
        return _FakeResponse(status_code, payload)

    def fake_post(url, json=None, **kwargs):
        captured["post_json"] = json
        return _FakeResponse(status_code, payload)

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)
    monkeypatch.setattr(app_mod.httpx, "post", fake_post)
    return captured


# --- patient-grant enforcement ---------------------------------------------


def test_create_request_for_an_ungranted_patient_is_denied(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"id": 1, "patient_id": UNGRANTED_PATIENT_ID}, status_code=201)

    resp = client.post(
        "/roi/requests",
        json={"patient_id": UNGRANTED_PATIENT_ID, "requested_by": "someone", "recipient": "Dr. X", "recipient_type": "provider"},
        headers=_auth(),
    )

    assert resp.status_code == 403


def test_create_request_for_a_granted_patient_succeeds(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"id": 1, "patient_id": GRANTED_PATIENT_ID}, status_code=201)

    resp = client.post(
        "/roi/requests",
        json={"patient_id": GRANTED_PATIENT_ID, "requested_by": "someone", "recipient": "Dr. X", "recipient_type": "provider"},
        headers=_auth(),
    )

    assert resp.status_code == 201


def test_fulfill_an_ungranted_patients_request_is_denied(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"request_id": 2, "patient_id": UNGRANTED_PATIENT_ID, "status": "fulfilled"})

    resp = client.post("/roi/requests/2/fulfill", json={"authorization_id": 2}, headers=_auth())

    assert resp.status_code == 403


def test_fulfill_a_granted_patients_request_succeeds(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"request_id": 1, "patient_id": GRANTED_PATIENT_ID, "status": "fulfilled"})

    resp = client.post("/roi/requests/1/fulfill", json={"authorization_id": 1}, headers=_auth())

    assert resp.status_code == 200


def test_fulfill_a_nonexistent_request_gets_the_same_403_as_ungranted(client, monkeypatch):
    """No existence oracle: a request_id that resolves to no row at all must
    not be distinguishable from one that exists but isn't granted."""
    _stub_downstream(monkeypatch, payload={})

    resp = client.post("/roi/requests/999999/fulfill", json={"authorization_id": 1}, headers=_auth())

    assert resp.status_code == 403
    assert resp.json() == client.post("/roi/requests/2/fulfill", json={"authorization_id": 2}, headers=_auth()).json()


def test_review_an_ungranted_patients_authorization_is_denied(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"id": 2, "patient_id": UNGRANTED_PATIENT_ID, "status": "valid"})

    resp = client.post("/roi/authorizations/2/review", json={"decision": "valid", "reviewed_by": "x"}, headers=_auth())

    assert resp.status_code == 403


def test_revoke_an_ungranted_patients_authorization_is_denied(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"id": 2, "patient_id": UNGRANTED_PATIENT_ID, "status": "revoked"})

    resp = client.post("/roi/authorizations/2/revoke", json={"revoked_by": "x"}, headers=_auth())

    assert resp.status_code == 403


def test_get_an_ungranted_patients_authorization_is_denied(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"id": 2, "patient_id": UNGRANTED_PATIENT_ID, "status": "valid"})

    resp = client.get("/roi/authorizations/2", headers=_auth())

    assert resp.status_code == 403


def test_accounting_for_an_ungranted_patient_is_denied(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"patient_id": UNGRANTED_PATIENT_ID, "disclosures": []})

    resp = client.get(f"/roi/patients/{UNGRANTED_PATIENT_ID}/accounting", headers=_auth())

    assert resp.status_code == 403


def test_listing_requests_for_an_ungranted_patient_is_denied(client, monkeypatch):
    _stub_downstream(monkeypatch, payload=[])

    resp = client.get(f"/roi/requests?patient_id={UNGRANTED_PATIENT_ID}", headers=_auth())

    assert resp.status_code == 403


def test_the_unscoped_operational_queue_list_needs_no_patient_grant(client, monkeypatch):
    """Deliberate: an roi_clerk's own triage queue across many patients they
    process releases for without a treatment-style grant — not a chart view."""
    _stub_downstream(monkeypatch, payload=[])

    resp = client.get("/roi/requests", headers=_auth())

    assert resp.status_code == 200


# --- actor identity is server-derived, never caller-supplied ----------------


def test_a_forged_requested_by_is_overridden_by_the_session_identity(client, monkeypatch):
    captured = _stub_downstream(monkeypatch, payload={"id": 1, "patient_id": GRANTED_PATIENT_ID}, status_code=201)

    resp = client.post(
        "/roi/requests",
        json={
            "patient_id": GRANTED_PATIENT_ID, "requested_by": "dr_someone_else_entirely",
            "recipient": "Dr. X", "recipient_type": "provider",
        },
        headers=_auth(),
    )

    assert resp.status_code == 201
    assert captured["post_json"]["requested_by"] == "roiclerk1"
    assert captured["post_json"]["requested_by"] != "dr_someone_else_entirely"


def test_a_forged_reviewed_by_is_overridden_by_the_session_identity(client, monkeypatch):
    captured = _stub_downstream(monkeypatch, payload={"id": 1, "patient_id": GRANTED_PATIENT_ID, "status": "valid"})

    resp = client.post(
        "/roi/authorizations/1/review",
        json={"decision": "valid", "reviewed_by": "supervisor_impersonation"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert captured["post_json"]["reviewed_by"] == "roiclerk1"


def test_a_forged_revoked_by_is_overridden_by_the_session_identity(client, monkeypatch):
    captured = _stub_downstream(monkeypatch, payload={"id": 1, "patient_id": GRANTED_PATIENT_ID, "status": "revoked"})

    resp = client.post(
        "/roi/authorizations/1/revoke",
        json={"revoked_by": "someone_impersonated"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert captured["post_json"]["revoked_by"] == "roiclerk1"


# --- downstream status codes reach the caller as themselves ----------------


def test_a_downstream_422_reaches_the_caller_as_422_not_a_false_200(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"detail": "authorization has expired"}, status_code=422)

    resp = client.post("/roi/requests/1/fulfill", json={"authorization_id": 1}, headers=_auth())

    assert resp.status_code == 422
    assert resp.json()["detail"] == "authorization has expired"


def test_a_downstream_409_reaches_the_caller_as_409_not_a_false_200(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"detail": "roi request has already been fulfilled"}, status_code=409)

    resp = client.post("/roi/requests/1/fulfill", json={"authorization_id": 1}, headers=_auth())

    assert resp.status_code == 409


def test_a_downstream_503_reaches_the_caller_as_503_not_a_false_200(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"detail": "database unavailable"}, status_code=503)

    resp = client.post(
        "/roi/requests",
        json={"patient_id": GRANTED_PATIENT_ID, "recipient": "Dr. X", "recipient_type": "provider"},
        headers=_auth(),
    )

    assert resp.status_code == 503
