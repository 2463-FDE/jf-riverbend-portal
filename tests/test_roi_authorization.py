"""w8-planner-2 P4 — persisted 164.508 authorization lifecycle, 164.522
restriction enforcement, and corrected 164.528 accounting classification
(services/roi-service/app.py, migration 030).

PR #93 (migration 029) was the foundation, not completion: it required an
authorization payload at fulfillment time, but trusted whatever
reference/signed_at/signed_by the caller asserted — Pydantic proved the
fields were non-empty, not that a real, reviewed, unexpired, unrevoked
authorization exists. This migration makes the authorization a real,
addressable, human-reviewed record (roi_authorizations) that
fulfill_roi_request loads and revalidates, adds a narrowly scoped
164.522 disclosure-restriction check, and corrects 029's test suite's
164.528 framing: disclosures gated by a valid 164.508 authorization are
EXEMPT from mandatory 164.528 accounting under 164.528(a)(2) — this
endpoint is an internal log with 164.528-shaped fields, not a substitute
for one.
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
OTHER_PATIENT_ID = 2099
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
        s.add(app_mod.Patient(id=OTHER_PATIENT_ID, name="Someone Else"))
        s.commit()

    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def _create_request(client, patient_id=PATIENT_ID, recipient=RECIPIENT, **overrides):
    payload = {
        "patient_id": patient_id,
        "requested_by": "front-desk-1",
        "recipient": recipient,
        "recipient_type": "provider",
        "purpose": "continuity of care",
        **overrides,
    }
    resp = client.post("/roi/requests", json=payload, headers=_headers())
    assert resp.status_code == 201
    return resp.json()["id"]


def _create_authorization(client, patient_id=PATIENT_ID, recipient=RECIPIENT, **overrides):
    payload = {
        "patient_id": patient_id,
        "recipient": recipient,
        "purpose": "continuity of care",
        "signature_evidence_reference": "signed-form-2026-0042",
        "signed_by": "Maria Gonzalez",
        "signed_at": "2026-08-20T14:30:00Z",
        **overrides,
    }
    resp = client.post("/roi/authorizations", json=payload, headers=_headers())
    assert resp.status_code == 201
    return resp.json()["id"]


def _review(client, authorization_id, decision="valid", reviewed_by="supervisor-1"):
    resp = client.post(
        f"/roi/authorizations/{authorization_id}/review",
        json={"decision": decision, "reviewed_by": reviewed_by},
        headers=_headers(),
    )
    assert resp.status_code == 200
    return resp.json()


def _valid_authorization(client, **overrides):
    auth_id = _create_authorization(client, **overrides)
    _review(client, auth_id, decision="valid")
    return auth_id


def _restrict(client, patient_id=PATIENT_ID, recipient=None, reason="patient requested"):
    resp = client.post(
        "/roi/restrictions",
        json={"patient_id": patient_id, "recipient": recipient, "reason": reason},
        headers=_headers(),
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _fulfill(client, request_id, authorization_id):
    return client.post(
        f"/roi/requests/{request_id}/fulfill",
        json={"authorization_id": authorization_id},
        headers=_headers(),
    )


# --- 1. legacy bypass is closed ------------------------------------------------


def test_the_legacy_disclosures_route_is_retired_even_with_a_valid_internal_token(client):
    resp = client.get(f"/disclosures/{PATIENT_ID}", headers=_headers())

    assert resp.status_code == 410
    # Never falls back to actually returning records — 410 must be the
    # whole response, not an error wrapper around real data.
    assert "records" not in resp.json()


# --- 2. authorization lifecycle: only a valid, reviewed record can be used ----


def test_fulfill_with_an_unknown_authorization_id_is_a_404(client):
    request_id = _create_request(client)

    resp = _fulfill(client, request_id, authorization_id=999999)

    assert resp.status_code == 404


def test_fulfill_with_a_pending_unreviewed_authorization_is_refused(client):
    request_id = _create_request(client)
    auth_id = _create_authorization(client)  # never reviewed — stays 'pending'

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 422


def test_fulfill_with_a_rejected_authorization_is_refused(client):
    request_id = _create_request(client)
    auth_id = _create_authorization(client)
    _review(client, auth_id, decision="rejected")

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 422


def test_fulfill_with_a_revoked_authorization_is_refused(client):
    request_id = _create_request(client)
    auth_id = _valid_authorization(client)
    revoke_resp = client.post(
        f"/roi/authorizations/{auth_id}/revoke", json={"revoked_by": "supervisor-1"}, headers=_headers()
    )
    assert revoke_resp.status_code == 200

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 422


def test_fulfill_with_an_expired_authorization_is_refused(client):
    request_id = _create_request(client)
    auth_id = _valid_authorization(client, expires_at="2020-01-01T00:00:00Z")

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 422


def test_fulfill_with_an_authorization_for_a_different_patient_is_refused(client):
    request_id = _create_request(client, patient_id=PATIENT_ID)
    auth_id = _valid_authorization(client, patient_id=OTHER_PATIENT_ID)

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 422


# --- 3. scope and recipient mismatch --------------------------------------------


def test_fulfill_with_a_recipient_mismatched_authorization_is_refused(client):
    request_id = _create_request(client, recipient="Dr. Chen, Riverbend East")
    auth_id = _valid_authorization(client, recipient="A Different Recipient, Elsewhere Clinic")

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 422


def test_fulfill_outside_the_authorizations_date_scope_is_refused(client):
    request_id = _create_request(
        client, date_range_start="2026-06-01", date_range_end="2026-06-30"
    )
    auth_id = _valid_authorization(client, scope_start="2026-07-01", scope_end="2026-12-31")

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 422


# --- 4. active restrictions block fulfillment -----------------------------------


def test_an_active_blanket_restriction_blocks_fulfillment(client):
    request_id = _create_request(client)
    auth_id = _valid_authorization(client)
    _restrict(client, patient_id=PATIENT_ID, recipient=None)

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 422


def test_a_restriction_scoped_to_a_different_recipient_does_not_block(client):
    request_id = _create_request(client, recipient=RECIPIENT)
    auth_id = _valid_authorization(client, recipient=RECIPIENT)
    _restrict(client, patient_id=PATIENT_ID, recipient="Some Other Recipient")

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 200


def test_a_revoked_restriction_no_longer_blocks_fulfillment(client):
    request_id = _create_request(client)
    auth_id = _valid_authorization(client)
    restriction_id = _restrict(client, patient_id=PATIENT_ID, recipient=None)
    revoke_resp = client.post(f"/roi/restrictions/{restriction_id}/revoke", headers=_headers())
    assert revoke_resp.status_code == 200

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 200


# --- 5. a fully valid authorization succeeds ------------------------------------


def test_fulfill_with_a_valid_matching_authorization_succeeds(client):
    request_id = _create_request(client)
    auth_id = _valid_authorization(client)

    resp = _fulfill(client, request_id, auth_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fulfilled"
    assert body["disclosure_id"] is not None


# --- 6. atomicity / idempotency: repeated fulfillment, one disclosure ----------


def test_repeated_fulfillment_of_the_same_request_does_not_create_a_second_disclosure(client):
    request_id = _create_request(client)
    auth_id = _valid_authorization(client)

    first = _fulfill(client, request_id, auth_id)
    assert first.status_code == 200

    second = _fulfill(client, request_id, auth_id)
    assert second.status_code == 409

    accounting = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers())
    assert len(accounting.json()["disclosures"]) == 1


# --- 7. disclosure ledger classification is correct -----------------------------


def test_the_accounting_entry_is_traceable_to_the_authorization_that_gated_it(client):
    request_id = _create_request(client)
    auth_id = _valid_authorization(client)

    _fulfill(client, request_id, auth_id)

    [entry] = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers()).json()["disclosures"]
    # Every row behind this endpoint is, by construction, gated by a valid
    # 164.508 authorization (fulfill_roi_request refuses any other path) —
    # and therefore exempt from mandatory 164.528 accounting under
    # 164.528(a)(2). authorization_id being populated is what proves that
    # classification for this row, not just a comment in the code.
    assert entry["authorization_id"] == auth_id
    assert entry["disclosed_to"] == RECIPIENT


def test_a_rejected_fulfillment_writes_no_disclosure_row(client):
    request_id = _create_request(client)
    auth_id = _create_authorization(client)  # left pending, never valid

    resp = _fulfill(client, request_id, auth_id)
    assert resp.status_code == 422

    accounting = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers())
    assert accounting.json()["disclosures"] == []


# --- 8. cross-patient accounting isolation --------------------------------------


def test_one_patients_accounting_never_includes_another_patients_disclosures(client):
    request_a = _create_request(client, patient_id=PATIENT_ID)
    auth_a = _valid_authorization(client, patient_id=PATIENT_ID)
    _fulfill(client, request_a, auth_a)

    request_b = _create_request(client, patient_id=OTHER_PATIENT_ID)
    auth_b = _valid_authorization(client, patient_id=OTHER_PATIENT_ID)
    _fulfill(client, request_b, auth_b)

    accounting_a = client.get(f"/roi/patients/{PATIENT_ID}/accounting", headers=_headers()).json()
    accounting_b = client.get(f"/roi/patients/{OTHER_PATIENT_ID}/accounting", headers=_headers()).json()

    assert len(accounting_a["disclosures"]) == 1
    assert len(accounting_b["disclosures"]) == 1
    assert accounting_a["disclosures"][0]["id"] != accounting_b["disclosures"][0]["id"]


# --- unaffected by this change: token guard, unknown request -------------------


def test_fulfilling_an_unknown_request_is_still_a_404_before_authorization_is_even_checked(client):
    resp = client.post(
        "/roi/requests/999999/fulfill", json={"authorization_id": 1}, headers=_headers()
    )

    assert resp.status_code == 404


def test_no_internal_token_is_still_refused_on_the_new_accounting_route(client):
    resp = client.get(f"/roi/patients/{PATIENT_ID}/accounting")

    assert resp.status_code == 401


def test_no_internal_token_is_refused_on_the_retired_legacy_route_too(client):
    resp = client.get(f"/disclosures/{PATIENT_ID}")

    assert resp.status_code == 401
