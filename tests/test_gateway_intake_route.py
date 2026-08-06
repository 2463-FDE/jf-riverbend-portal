"""Tests for the gateway's /intake proxy (services/gateway/app.py::proxy_intake).

PR #20 round-8 review: the route used to call `_post` without
forward_status=True, so intake-service's 409 duplicate-patient response was
flattened into a bare 200 — the frontend read a blocked, no-op submission as
a success. These tests check that upstream status codes are now forwarded
faithfully, mirroring the existing coverage for the eligibility proxy routes
in tests/test_gateway_eligibility_routes.py.

Round-11 review: also covers that proxy_intake forwards X-Internal-Token —
the shared secret intake-service now verifies before trusting that a call
came through the gateway (mirrors proxy_patient_view's identical fix).
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_intake")

VALID_TOKEN = "valid-token-abc"
_VALID_SESSION = {"username": "frontdesk", "role": "staff"}
TEST_INTERNAL_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


@pytest.fixture
def client(monkeypatch):
    def fake_get_session(token):
        return _VALID_SESSION if token == VALID_TOKEN else None

    monkeypatch.setattr(app_mod, "get_session", fake_get_session)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)
    return TestClient(app_mod.app)


def _auth():
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_intake_rejects_anonymous_callers(client):
    resp = client.post("/intake", json={"demographics": {"name": "Jane Roe"}})

    assert resp.status_code == 401


def test_intake_success_is_forwarded_as_201(client, monkeypatch):
    body = {"patient_id": 7, "elapsed_seconds": 0.1, "possible_duplicate_match": False}
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/intake")
        captured["headers"] = headers
        return _FakeResponse(201, body)

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/intake", json={"demographics": {"name": "Jane Roe"}}, headers=_auth())

    assert resp.status_code == 201
    assert resp.json() == body
    # Round-11 review: proves this call to intake-service carries the gateway's
    # shared secret, not just a bare forwarded payload.
    assert captured["headers"]["X-Internal-Token"] == TEST_INTERNAL_TOKEN


def test_intake_duplicate_conflict_is_forwarded_as_409_not_flattened_to_200(client, monkeypatch):
    # This is the exact defect the round-8 review flagged: before
    # forward_status=True, this 409 reached the frontend as a 200 with no
    # patient/coverage/consent rows actually created.
    body = {"detail": {"error": "possible_duplicate_patient", "confidence": "exact"}}

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(409, body)

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/intake", json={"demographics": {"name": "Jane Roe"}}, headers=_auth())

    assert resp.status_code == 409
    assert resp.json() == body


def test_intake_downstream_unreachable_is_a_502_not_a_bare_200(client, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/intake", json={"demographics": {"name": "Jane Roe"}}, headers=_auth())

    assert resp.status_code == 502
