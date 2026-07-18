"""
Integration test for the Stage 3 async eligibility path — requires the full
stack up (`make up`) on localhost, exactly like test_records_flow.py.

Drives the real gateway -> intake-service -> eligibility-service path
end-to-end: /intake must return promptly (RIV-088/RIV-141 fix) with a
pending status and a job id, and that job must be pollable through the
gateway's authenticated status route. This repo's own .env points
PAYER_API_URL at an unreachable placeholder clearinghouse domain, so the
realistic terminal state here is DEAD_LETTER (retries exhausted) rather than
a genuine active/inactive answer — this test asserts the JOB LIFECYCLE
completes safely, not a specific coverage verdict.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).
"""
import os
import time

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")


def _token() -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": "frontdesk", "password": "portal123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


def test_intake_returns_promptly_with_a_pending_job(monkeypatch=None):
    payload = {
        "demographics": {"name": "Integration Test Patient", "dob": "1990-01-01"},
        "insurance": {"payer_name": "Aetna", "member_id": "INTEGRATION-TEST-MEM"},
        "consents": ["npp_ack", "treatment_consent"],
    }
    started = time.time()
    r = httpx.post(f"{GATEWAY}/intake", json=payload, headers=_auth_headers(), timeout=10)
    wall_clock = time.time() - started

    assert r.status_code in (200, 201)
    body = r.json()
    assert body["eligibility_status"] == "pending"
    assert body["eligibility_job_id"]
    assert body["eligibility"]["status"] == "pending"  # backward-compat field preserved
    # Nowhere near the old RIV-088 "~4-5s" spin (let alone RIV-141's ~20 min) —
    # this bounds the regression, it does not require a specific fast number.
    assert wall_clock < 5.0


def test_eligibility_job_status_is_pollable_through_the_gateway_and_eventually_settles():
    payload = {
        "demographics": {"name": "Integration Test Patient 2", "dob": "1990-01-01"},
        "insurance": {"payer_name": "Aetna", "member_id": "INTEGRATION-TEST-MEM-2"},
        "consents": ["npp_ack", "treatment_consent"],
    }
    headers = _auth_headers()
    intake = httpx.post(f"{GATEWAY}/intake", json=payload, headers=headers, timeout=10).json()
    job_id = intake["eligibility_job_id"]

    terminal_statuses = {"succeeded", "dead_letter"}
    settled = None
    for _ in range(20):
        resp = httpx.get(f"{GATEWAY}/eligibility/jobs/{job_id}", headers=headers, timeout=10)
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in terminal_statuses:
            settled = body
            break
        time.sleep(1)

    assert settled is not None, "job never reached a terminal state within the poll budget"
    # Never silently dropped: a dead-lettered job still carries an error TYPE,
    # never a raw exception message.
    if settled["status"] == "dead_letter":
        assert settled["error"]
        assert "INTEGRATION-TEST-MEM-2" not in (settled["error"] or "")


def test_eligibility_job_status_requires_authentication():
    r = httpx.get(f"{GATEWAY}/eligibility/jobs/some-job-id", timeout=10)
    assert r.status_code == 401


def test_visit_chat_endpoint_requires_auth_and_degrades_safely_when_authenticated():
    r = httpx.post(f"{GATEWAY}/visits/integration-visit-1/messages", json={"message": "hi"}, timeout=10)
    assert r.status_code == 401

    r = httpx.post(
        f"{GATEWAY}/visits/integration-visit-1/messages",
        json={"message": "am I covered?"},
        headers=_auth_headers(),
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    # No live Bedrock credential in this repo (BEDROCK_MODEL_ID=changeme) —
    # the real, expected behavior here is a safe degraded reply, never a 500.
    assert body["termination_reason"] in ("provider_error", "answered", "max_turns")
    assert body["reply"]
