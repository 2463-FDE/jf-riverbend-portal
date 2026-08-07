"""Integration tests — require the full stack up (`make up`) on localhost.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).

Stage 3 — end-to-end GET /patients/{id}/view (gateway -> records-service),
against seeded demo data only (db/seed/patients.csv: 1042 Maria Gonzalez,
1043 James O'Brien). Confirms the route's actual behavior.

Week 4 catch-up: /patients/{id}/view is now gated by the same
SqlPatientAccessGate as the sibling /patients/{id}/records route (real
per-(actor, patient) grants — db/seed/generate_seed.py seeds frontdesk for
1042 but NOT 1043), replacing the earlier authenticated-staff-only
StaffAccessGate this file used to document. test_gate_is_authenticated_
staff_not_patient_specific and test_legacy_idor_endpoint_is_unchanged_by_
this_route below were rewritten to prove the OPPOSITE of what their old
names claimed.

Also covers the review fix (round, 2026-08-05): records-service's host port
is published (docker-compose.yml), so a caller can reach it directly,
bypassing the gateway entirely. test_direct_call_with_spoofed_actor_is_rejected
proves that path is closed even though the gateway path above still works —
this requires INTERNAL_SERVICE_TOKEN to be set identically on both gateway
and records-service in .env; if it isn't, every request in this file 401s,
including the ones that are supposed to succeed, which is itself a proof the
fix fails closed rather than silently reopening the bypass.
"""
import os

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")
RECORDS = os.getenv("RECORDS_URL", "http://localhost:8073")


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


def test_gate_is_now_patient_specific_not_just_authenticated_staff():
    # Week 4 catch-up: this replaces the old
    # test_gate_is_authenticated_staff_not_patient_specific, which proved
    # the OPPOSITE of what its new name says — the same frontdesk session
    # that can view 1042 could ALSO view an unrelated patient (1043)
    # through this route, because StaffAccessGate made no ownership claim.
    # SqlPatientAccessGate denies it: frontdesk has no patient_access_grants
    # row for 1043 (db/seed/generate_seed.py).
    headers = {"Authorization": f"Bearer {_token()}"}
    r = httpx.get(f"{GATEWAY}/patients/1043/view", headers=headers, timeout=10)
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "not_authorized"


def test_a_different_user_granted_1043_can_view_it():
    # The other half: drpatel IS granted 1043 (seeded as James O'Brien's
    # physician) — same route, different actor, opposite outcome from
    # frontdesk above.
    headers = {"Authorization": f"Bearer {_token('drpatel')}"}
    r = httpx.get(f"{GATEWAY}/patients/1043/view", headers=headers, timeout=10)
    assert r.status_code == 200
    assert r.json()["patient_id"] == 1043


def test_direct_call_with_spoofed_actor_is_rejected():
    # The exact bypass the review flagged: records-service is reachable
    # directly on its published host port, so a caller could try to skip the
    # gateway entirely and hand-craft X-Actor-Id. Must 401 — the internal
    # token check runs before StaffAccessGate ever sees this request.
    r = httpx.get(f"{RECORDS}/patients/1042/view", headers={"X-Actor-Id": "attacker"}, timeout=10)
    assert r.status_code == 401


def test_legacy_idor_endpoint_is_now_patient_scoped_too():
    # Week 4 catch-up: this replaces the old
    # test_legacy_idor_endpoint_is_unchanged_by_this_route, which documented
    # docs/analysis/RIV-201-patient-records-IDOR.md's finding as still true
    # — a cross-patient request used to return 200 with no ownership check
    # at all. It's fixed now; see tests/integration/test_records_flow.py::
    # test_user_cannot_read_other_patients_chart for the equivalent
    # regression test living alongside the rest of that endpoint's coverage.
    headers = {"Authorization": f"Bearer {_token()}"}
    r = httpx.get(f"{GATEWAY}/patients/1043/records", headers=headers, timeout=10)
    assert r.status_code == 403
