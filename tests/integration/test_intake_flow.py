"""
Integration tests — require the full stack up (`make up`) on localhost.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).

Round-11 review: /intake's duplicate-detection response (409 exact match /
201+possible_duplicate_match=true partial match / plain 201 no match) is a
patient/SSN-existence oracle for any unauthenticated caller. The gateway
already requires a staff session before forwarding to /intake
(services/gateway/app.py::proxy_intake) — test_intake_via_gateway_still_succeeds
below proves that path is unaffected. The actual gap was that intake-service
itself (published on host port 8071, docker-compose.yml) had no way to tell a
genuine gateway-forwarded call apart from a direct one bypassing that session
check entirely — test_direct_call_to_intake_service_is_rejected proves that
bypass is now closed.
"""
import os
import uuid

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")
INTAKE = os.getenv("INTAKE_URL", "http://localhost:8071")


def _token() -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": "frontdesk", "password": "portal123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _unique_demographics() -> dict:
    # A fresh, never-before-seen ssn each run so this test's own submissions
    # never collide with seed data or earlier runs as an exact/partial match
    # — this test is about the auth gate, not the match-key logic itself.
    suffix = uuid.uuid4().hex[:9]
    return {"name": "Integration TestCheck", "ssn": suffix, "dob": "1970-01-01"}


def test_intake_via_gateway_still_succeeds():
    headers = {"Authorization": f"Bearer {_token()}"}
    payload = {"demographics": _unique_demographics(), "consents": ["npp_ack", "treatment_consent"]}

    r = httpx.post(f"{GATEWAY}/intake", json=payload, headers=headers, timeout=10)

    assert r.status_code == 201
    assert r.json()["patient_id"]


def test_direct_call_to_intake_service_is_rejected():
    # The bypass the review flagged: hitting intake-service directly on its
    # published host port, skipping the gateway's require_session entirely.
    payload = {"demographics": _unique_demographics(), "consents": ["npp_ack", "treatment_consent"]}

    r = httpx.post(f"{INTAKE}/intake", json=payload, timeout=10)

    assert r.status_code == 401
