"""Integration tests — require the full stack up (`make up`) on localhost.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).

Stage 3 — end-to-end GET /patients/{id}/view (gateway -> records-service),
against seeded demo data only (db/seed/patients.csv: 1042 Maria Gonzalez,
1043 James O'Brien). Confirms the new route's actual behavior, and that it
leaves the sibling IDOR endpoint's documented behavior (tested in
test_records_flow.py) untouched.
"""
import os

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")


def _token(username: str = "frontdesk", password: str = "portal123") -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def test_view_requires_authentication():
    r = httpx.get(f"{GATEWAY}/patients/1042/view", timeout=10)
    assert r.status_code == 401


def test_authenticated_staff_gets_a_completed_view_with_evidence():
    headers = {"Authorization": f"Bearer {_token()}"}
    r = httpx.get(f"{GATEWAY}/patients/1042/view", headers=headers, timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["patient_id"] == 1042
    assert body["outcome"] in ("completed", "escalated")
    assert isinstance(body["evidence_ids"], list)
    # PHI-free citation handles only (e.g. "encounter:1"), never a raw name/SSN.
    for evidence_id in body["evidence_ids"]:
        assert evidence_id.split(":")[0] in ("patient", "encounter", "provider", "record")


def test_gate_is_authenticated_staff_not_patient_specific():
    # The defining property of Stage 3's scope decision: the SAME frontdesk
    # session that can view 1042 above can also view an unrelated patient
    # (1043) through this route — this route makes no ownership claim, unlike
    # the sibling /patients/{id}/records IDOR this deliberately does not fix.
    headers = {"Authorization": f"Bearer {_token()}"}
    r = httpx.get(f"{GATEWAY}/patients/1043/view", headers=headers, timeout=10)
    assert r.status_code == 200
    assert r.json()["patient_id"] == 1043


def test_legacy_idor_endpoint_is_unchanged_by_this_route():
    # docs/analysis/RIV-201-patient-records-IDOR.md: this route must not be
    # described as fixing RIV-201. Confirm the legacy endpoint still returns
    # 200 for a cross-patient request with no ownership check — same
    # behavior test_records_flow.py's xfail already documents.
    headers = {"Authorization": f"Bearer {_token()}"}
    r = httpx.get(f"{GATEWAY}/patients/1043/records", headers=headers, timeout=10)
    assert r.status_code == 200
