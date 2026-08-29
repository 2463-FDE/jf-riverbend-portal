"""Tests for the gateway's /intake/instructions proxy
(services/gateway/app.py::proxy_intake_instructions), Stage 1 — feature
readiness. Mirrors tests/test_gateway_intake_route.py's coverage: session
required, X-Internal-Token forwarded, upstream status/errors passed through
faithfully.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import install_sqlite_users_db, load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_intake_instructions")

VALID_TOKEN = "valid-token-abc"
_VALID_SESSION = {"user_id": "2", "username": "frontdesk", "role": "staff", "security_version": "0"}
TEST_INTERNAL_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


@pytest.fixture
def client(monkeypatch):
    def fake_get_session(token):
        return _VALID_SESSION if token == VALID_TOKEN else None

    monkeypatch.setattr(app_mod, "get_session", fake_get_session)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)
    install_sqlite_users_db(app_mod, [
        app_mod.User(id=2, username="frontdesk", password_hash="x", role="staff", is_active=True),
    ])
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


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
    resp = client.post("/intake/instructions", json={"step": "demographics"})

    assert resp.status_code == 401


def test_success_forwards_internal_token_and_body(client, monkeypatch):
    body = {"summary": "Fill in your name and date of birth.", "used_fallback": True}
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/intake/instructions")
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(200, body)

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/intake/instructions", json={"step": "demographics"}, headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == body
    assert captured["headers"]["X-Internal-Token"] == TEST_INTERNAL_TOKEN
    assert captured["json"] == {"step": "demographics"}


def test_upstream_validation_error_is_forwarded_not_flattened(client, monkeypatch):
    body = {"detail": [{"msg": "unknown step"}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(422, body)

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/intake/instructions", json={"step": "not-a-step"}, headers=_auth())

    assert resp.status_code == 422
    assert resp.json() == body


def test_downstream_unreachable_is_a_502(client, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/intake/instructions", json={"step": "demographics"}, headers=_auth())

    assert resp.status_code == 502
