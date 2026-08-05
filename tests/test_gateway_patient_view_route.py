"""Stage 3 — services/gateway/app.py::proxy_patient_view.

Mirrors tests/test_gateway_eligibility_routes.py's pattern: fake the Redis
session lookup and httpx call, then check auth gating and that the session's
username/correlation id are forwarded, upstream status codes are not
flattened, and the client-supplied purpose is passed through untouched.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_patient_view")

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


def test_anonymous_caller_is_rejected(client):
    resp = client.get("/patients/1042/view")
    assert resp.status_code == 401


def test_forwards_actor_id_from_session_and_a_correlation_header(client, monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(200, {"outcome": "completed", "patient_id": 1042})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients/1042/view", headers=_auth())

    assert resp.status_code == 200
    assert captured["url"].endswith("/patients/1042/view")
    assert captured["headers"]["X-Actor-Id"] == "frontdesk"
    assert "X-Request-Id" in captured["headers"]
    assert len(captured["headers"]["X-Request-Id"]) == 32


def test_denial_status_is_forwarded_not_flattened_to_200(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(403, {"detail": {"reason": "unknown_actor", "correlation_id": "cid-1"}})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients/1042/view", headers=_auth())

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "unknown_actor"


def test_downstream_unreachable_is_a_502_not_a_bare_200(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients/1042/view", headers=_auth())

    assert resp.status_code == 502


def test_purpose_query_param_is_passed_through(client, monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _FakeResponse(200, {"outcome": "completed", "patient_id": 1042})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients/1042/view", params={"purpose": "payment"}, headers=_auth())

    assert resp.status_code == 200
    assert captured["params"]["purpose"] == "payment"
