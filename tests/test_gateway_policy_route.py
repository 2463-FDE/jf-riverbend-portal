"""Tests for the gateway's /policy/ask proxy
(services/gateway/app.py::proxy_ask_policy_navigator) — mirrors
tests/test_gateway_intake_instructions_route.py: session required (any
authenticated role, no data-type permission — this never touches patient
data), X-Actor-Id/X-Internal-Token forwarded, only `question` taken from
the request body, upstream status/errors passed through faithfully.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_policy_route")

VALID_TOKEN = "valid-token-abc"
_VALID_SESSION = {"user_id": "42", "username": "drkim", "role": "clinician"}
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


def test_rejects_anonymous_callers(client):
    resp = client.post("/policy/ask", json={"question": "How does intake work?"})

    assert resp.status_code == 401


def test_success_forwards_actor_id_and_only_the_question(client, monkeypatch):
    body = {"answer": "Consent is captured before registration.", "citations": [], "label": "real",
            "termination_reason": "answered"}
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/policy/ask")
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(200, body)

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post(
        "/policy/ask",
        json={"question": "How does intake work?", "role": "smuggled-admin"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert resp.json() == body
    assert captured["headers"]["X-Internal-Token"] == TEST_INTERNAL_TOKEN
    assert captured["headers"]["X-Actor-Id"] == "42"
    # Only `question` is ever forwarded — a caller-supplied "role" is dropped,
    # never trusted (records-service re-derives role from X-Actor-Id itself).
    assert captured["json"] == {"question": "How does intake work?"}


def test_upstream_error_is_forwarded_not_flattened(client, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(422, {"detail": "question must not be blank"})

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/policy/ask", json={"question": ""}, headers=_auth())

    assert resp.status_code == 422


def test_downstream_unreachable_is_a_502(client, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/policy/ask", json={"question": "anything"}, headers=_auth())

    assert resp.status_code == 502
