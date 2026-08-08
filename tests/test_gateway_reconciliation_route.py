"""Stage 2 (Week 6) — services/gateway/app.py::proxy_patient_reconciliation.

Mirrors tests/test_gateway_patient_view_route.py's pattern (same fake Redis
session lookup / fake httpx.get), since this route uses the identical trust
model (X-Actor-Id + X-Internal-Token). No `purpose` param on this route.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_reconciliation")

VALID_TOKEN = "valid-token-abc"
_VALID_SESSION = {"user_id": "2", "username": "frontdesk", "role": "staff"}
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


def test_anonymous_caller_is_rejected(client):
    resp = client.get("/patients/1042/reconciliation")
    assert resp.status_code == 401


def test_forwards_actor_id_and_internal_token(client, monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResponse(200, {"patient_id": 1042, "source_records": []})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients/1042/reconciliation", headers=_auth())

    assert resp.status_code == 200
    assert captured["url"].endswith("/patients/1042/reconciliation")
    # Consolidation review (PR #22): the numeric users.id must reach records-
    # service (username would be rejected by parse_user_id -> 403 for every real
    # user). username rides along only as X-Actor-Name for audit.
    assert captured["headers"]["X-Actor-Id"] == "2"
    assert captured["headers"]["X-Actor-Name"] == "frontdesk"
    assert captured["headers"]["X-Internal-Token"] == TEST_INTERNAL_TOKEN
    assert "X-Request-Id" in captured["headers"]


def test_denial_status_is_forwarded_not_flattened_to_200(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(403, {"detail": {"reason": "unknown_actor", "correlation_id": "cid-1"}})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients/1042/reconciliation", headers=_auth())

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "unknown_actor"


def test_downstream_unreachable_is_a_502_not_a_bare_200(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients/1042/reconciliation", headers=_auth())

    assert resp.status_code == 502
