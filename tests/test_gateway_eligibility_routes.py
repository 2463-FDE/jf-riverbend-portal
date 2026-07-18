"""Tests for the gateway's Stage 3 proxy routes: eligibility job status/
retry and visit-scoped assistant turns (services/gateway/app.py).

Same auth posture as every other gateway route — Depends(require_session) —
so these tests check both "no unauthenticated exposure was added" and that
upstream status codes (404/409/503) are faithfully forwarded rather than
flattened to a blanket 200, which the frontend polling surface depends on.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app")

VALID_TOKEN = "valid-token-abc"
_VALID_SESSION = {"username": "frontdesk", "role": "staff"}


@pytest.fixture
def client(monkeypatch):
    def fake_get_session(token):
        return _VALID_SESSION if token == VALID_TOKEN else None

    monkeypatch.setattr(app_mod, "get_session", fake_get_session)
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


# --- auth gating: no new unauthenticated exposure -----------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/eligibility/jobs/job-1"),
        ("post", "/eligibility/jobs/job-1/retry"),
        ("post", "/visits/visit-1/messages"),
    ],
)
def test_new_routes_reject_anonymous_callers(client, method, path):
    kwargs = {"json": {"message": "hi"}} if method == "post" and "messages" in path else {}
    resp = getattr(client, method)(path, **kwargs)

    assert resp.status_code == 401


# --- job status: forwards upstream status + body ------------------------------


def test_job_status_ok_is_proxied_through(client, monkeypatch):
    body = {"job_id": "job-1", "status": "queued", "retry_count": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "X-Request-Id" in headers
        assert url.endswith("/eligibility/jobs/job-1")
        return _FakeResponse(200, body)

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/eligibility/jobs/job-1", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == body


def test_job_status_404_is_forwarded_not_flattened_to_200(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(404, {"detail": "job not found"})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/eligibility/jobs/does-not-exist", headers=_auth())

    assert resp.status_code == 404


def test_job_status_downstream_unreachable_is_a_502_not_a_bare_200(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/eligibility/jobs/job-1", headers=_auth())

    assert resp.status_code == 502


# --- retry: forwards 409 conflict ---------------------------------------------


def test_retry_conflict_is_forwarded_as_409(client, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/eligibility/jobs/job-1/retry")
        return _FakeResponse(409, {"job_id": "job-1", "status": "queued"})

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/eligibility/jobs/job-1/retry", headers=_auth())

    assert resp.status_code == 409


def test_retry_success_is_forwarded_as_200(client, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(200, {"job_id": "job-1", "status": "retryable"})

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/eligibility/jobs/job-1/retry", headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["status"] == "retryable"


# --- visit-chat: correlation id present, payload passed through ---------------


def test_visit_message_forwards_payload_and_a_correlation_header(client, monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200, {"visit_id": "visit-1", "reply": "ok", "tool_called": False,
                                    "termination_reason": "answered", "turns_used": 1})

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post(
        "/visits/visit-1/messages", json={"message": "am I covered?"}, headers=_auth()
    )

    assert resp.status_code == 200
    assert captured["url"].endswith("/visits/visit-1/messages")
    assert captured["json"] == {"message": "am I covered?"}
    assert "X-Request-Id" in captured["headers"]
    # Opaque, uuid4-hex shaped — not derived from the session/username.
    correlation_id = captured["headers"]["X-Request-Id"]
    assert len(correlation_id) == 32
    assert "frontdesk" not in correlation_id
