"""PR #23 review round 2 (2026-08-07) — services/gateway/app.py::proxy_patients
(GET /patients). records-service now scopes the roster/name-search to the
caller's active grants, so the gateway MUST forward the actor
(X-Actor-Id=user_id, X-Actor-Name=username) or records-service sees no actor
and returns an empty PatientPage — every real user gets total=0 even with
grants. The review's high finding was that this failure is silent (an empty
page looks like success), so this test locks the forwarding in.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import install_sqlite_users_db, load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_patients_list")

VALID_TOKEN = "valid-token-abc"
_VALID_SESSION = {"user_id": "2", "username": "frontdesk", "role": "staff", "security_version": "0"}
TEST_INTERNAL_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        app_mod, "get_session", lambda t: _VALID_SESSION if t == VALID_TOKEN else None
    )
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


def test_anonymous_caller_is_rejected(client):
    assert client.get("/patients").status_code == 401


def test_forwards_actor_headers_so_the_roster_is_not_empty(client, monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return _FakeResponse(200, {"items": [{"id": 1042}], "total": 1, "limit": 25, "offset": 0})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients", params={"q": "gonz"}, headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["total"] == 1  # non-empty roster reaches the caller
    # The fix: the actor is forwarded, so records-service can apply the grant filter.
    assert captured["headers"]["X-Actor-Id"] == "2"          # stable users.id
    assert captured["headers"]["X-Actor-Name"] == "frontdesk"  # username, audit only
    assert captured["headers"]["X-Internal-Token"] == TEST_INTERNAL_TOKEN
    assert captured["params"]["q"] == "gonz"


def test_upstream_status_is_forwarded_not_flattened(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(503, {"detail": "database unavailable"})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/patients", headers=_auth())
    assert resp.status_code == 503  # not silently flattened to 200
