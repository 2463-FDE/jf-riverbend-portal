"""W10 Final Stage 2 — services/roi-service/app.py::fulfill_roi_request's
remaining concurrent-duplicate-fulfillment gap.

Real concurrency (two threads racing the same request through a real
Postgres FOR UPDATE lock) is proved separately, against a live database, in
tests/integration/test_roi_fulfillment_concurrency.py. This file is the
fast, DB-less unit coverage of the two pieces that fix makes app.py
responsible for: the row lock is actually requested, and a unique-index
violation (migration 035's database-level backstop) is turned into the
same truthful 409 an ordinary already-fulfilled request gets, never a raw
500.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/roi-service/app.py", "roi_fulfillment_concurrency_app")

TOKEN = "test-internal-token-for-roi-well-over-32-characters"
PATIENT_ID = 1042
RECIPIENT = "Dr. Chen, Riverbend East"


def _headers():
    return {"X-Internal-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    app_mod.Patient.metadata.create_all(engine)
    # Mirrors migration 035 exactly (SQLite supports the same partial-index
    # syntax) — without this, the app-level fix would have nothing to
    # actually catch.
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


def test_the_read_before_fulfilling_takes_a_row_lock():
    """Confirms the fix that closes the race: fulfill_roi_request no longer
    plain-reads the request row — a locked SELECT is what makes a second,
    truly concurrent request block instead of racing past the status check."""
    import inspect

    source = inspect.getsource(app_mod.fulfill_roi_request)
    assert "with_for_update()" in source


def test_a_disclosure_already_present_for_this_request_id_yields_a_truthful_409(client, monkeypatch):
    """Simulates the outcome the lock exists to prevent: a disclosure row
    for this roi_request_id already exists (as if a racing request won
    first) while the request itself is still 'pending' — the ordinary
    status check can't catch this, only the unique index can. Proves the
    resulting IntegrityError is mapped to the same 409 an ordinary
    already-fulfilled request gets, not a raw 500."""
    client_obj, Session = client
    request_id = _create_request(client_obj)
    auth_id = _valid_authorization(client_obj)

    with Session() as s:
        s.add(app_mod.Disclosure(patient_id=PATIENT_ID, roi_request_id=request_id, disclosed_to=RECIPIENT))
        s.commit()

    resp = client_obj.post(
        f"/roi/requests/{request_id}/fulfill",
        json={"authorization_id": auth_id},
        headers=_headers(),
    )

    assert resp.status_code == 409
    assert "already been fulfilled" in resp.json()["detail"]

    with Session() as s:
        count = s.query(app_mod.Disclosure).filter_by(roi_request_id=request_id).count()
    assert count == 1, "the conflicting insert must never land a second disclosure row"
