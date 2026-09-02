"""Demo-readiness slice: `dwhite` (Dana White), a least-privilege ROI demo
identity on the real `roi_clerk` role (config/roles.yaml), scoped to exactly
one active patient_access_grants row (patient 1042) — see
db/seed/generate_seed.py and db/seed/demo_reset.sql for the seed/reset side.

This exercises the SAME trust-boundary mechanism
test_gateway_roi_authorization.py already covers generically (any
`roi_clerk` session + a grant), but pinned to the literal demo identity and
scope this PR ships, plus the property that identity alone never grants
clinical-record access: `roi_clerk` in config/roles.yaml holds no
`records.read`/`records.write`, so /patients/{id}/records must 403 for
`dwhite` regardless of any patient_access_grants row.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_dwhite_roi_identity")

VALID_TOKEN = "valid-token-abc"
DWHITE_USER_ID = 14  # matches db/seed/generate_seed.py's USERS-list position
GRANTED_PATIENT_ID = 1042
UNGRANTED_PATIENT_ID = 1043


def _session() -> dict:
    return {"user_id": str(DWHITE_USER_ID), "username": "dwhite", "role": "roi_clerk", "security_version": "0"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "test-internal-token-well-over-the-32-char-floor")
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
        s.add(app_mod.User(id=DWHITE_USER_ID, username="dwhite", password_hash="x", role="roi_clerk", is_active=True))
        # dwhite's ONE grant — 1042 only, matching the seed/reset scope this PR ships.
        s.add(app_mod.PatientAccessGrant(user_id=DWHITE_USER_ID, patient_id=GRANTED_PATIENT_ID))
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


def test_dwhite_can_create_an_roi_request_for_1042(client, monkeypatch):
    monkeypatch.setattr(
        app_mod.httpx, "post",
        lambda url, json=None, **kw: _FakeResponse(201, {"id": 1, "patient_id": GRANTED_PATIENT_ID}),
    )

    resp = client.post(
        "/roi/requests",
        json={"patient_id": GRANTED_PATIENT_ID, "recipient": "Dr. X", "recipient_type": "provider"},
        headers=_auth(),
    )

    assert resp.status_code == 201


def test_dwhite_is_denied_for_a_patient_outside_the_scoped_grant(client, monkeypatch):
    monkeypatch.setattr(
        app_mod.httpx, "post",
        lambda url, json=None, **kw: _FakeResponse(201, {"id": 2, "patient_id": UNGRANTED_PATIENT_ID}),
    )

    resp = client.post(
        "/roi/requests",
        json={"patient_id": UNGRANTED_PATIENT_ID, "recipient": "Dr. X", "recipient_type": "provider"},
        headers=_auth(),
    )

    assert resp.status_code == 403


def test_dwhite_cannot_read_clinical_records_even_for_the_granted_patient(client, monkeypatch):
    """roi_clerk holds no records.read/records.write (config/roles.yaml) —
    the grant scopes WHICH patient's ROI actions dwhite may perform, it does
    not itself unlock the chart. This must 403 before any downstream call,
    so a downstream stub that would incorrectly succeed proves the point."""
    monkeypatch.setattr(
        app_mod.httpx, "get",
        lambda url, **kw: _FakeResponse(200, {"patient_id": GRANTED_PATIENT_ID, "encounters": [{"id": 1}]}),
    )

    resp = client.get(f"/patients/{GRANTED_PATIENT_ID}/records", headers=_auth())

    assert resp.status_code == 403
