"""
Integration tests — require the full stack up (`make up`) on localhost.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).
"""
import os

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")


def _token() -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": "frontdesk", "password": "portal123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _token_for(username: str) -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": "portal123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def test_login_returns_token():
    assert _token()


def test_records_require_authentication():
    # No bearer token -> 401 (anonymous access is rejected at the gateway).
    r = httpx.get(f"{GATEWAY}/patients/1042/records", timeout=10)
    assert r.status_code == 401


def test_authenticated_user_can_read_a_chart():
    headers = {"Authorization": f"Bearer {_token()}"}
    r = httpx.get(f"{GATEWAY}/patients/1042/records", headers=headers, timeout=10)
    assert r.status_code == 200
    assert r.json()["patient_id"] == 1042


def test_user_cannot_read_other_patients_chart():
    # Week 4 catch-up (RIV-201 / DEBT D11 fix): this xfail is now a real,
    # passing regression test. frontdesk is seeded with a patient_access_grants
    # row for 1042 but NOT 1043 (db/seed/generate_seed.py) — pulling an
    # unrelated chart is now actually forbidden, not just documented as should-be.
    headers = {"Authorization": f"Bearer {_token()}"}
    r = httpx.get(f"{GATEWAY}/patients/1043/records", headers=headers, timeout=10)
    assert r.status_code == 403


def test_user_can_read_a_patient_they_are_granted() -> None:
    # The other half of the same fact: frontdesk IS granted 1042, so this
    # must keep working — same login, different chart, opposite outcome.
    headers = {"Authorization": f"Bearer {_token()}"}
    r = httpx.get(f"{GATEWAY}/patients/1042/records", headers=headers, timeout=10)
    assert r.status_code == 200
    assert r.json()["patient_id"] == 1042


def test_registration_user_can_review_the_seeded_duplicate_cluster() -> None:
    # rdelgado is the duplicate-records demo user. Reconciliation authorizes
    # each matching chart independently, so all three Maria Gonzalez rows
    # must be granted for the related-record view to show the discrepancy.
    headers = {"Authorization": f"Bearer {_token_for('rdelgado')}"}
    r = httpx.get(f"{GATEWAY}/patients/1042/reconciliation", headers=headers, timeout=10)

    assert r.status_code == 200
    body = r.json()
    assert {record["patient_id"] for record in body["source_records"]} == {1042, 1330, 1588}
    assert any(
        discrepancy["category"] == "allergy" and discrepancy["value"] == "penicillin"
        for discrepancy in body["discrepancies"]
    )
