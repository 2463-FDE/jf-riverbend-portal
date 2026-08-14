"""POST /logout — shared-workstation fix.

The client's ask was "a logout that actually ends the session server-side."
The failure mode this locks out: logout reports success while the session is
still live, which is what let the frontend show a signed-out screen over a
usable session on a machine the next person was about to use.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_logout")


@pytest.fixture
def client():
    return TestClient(app_mod.app)


def test_logout_destroys_the_session_and_says_so(client, monkeypatch):
    destroyed = []
    monkeypatch.setattr(app_mod, "destroy_session", lambda t: destroyed.append(t) or True)

    resp = client.post("/logout", headers={"Authorization": "Bearer tok-abc"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "session_ended": True}
    assert destroyed == ["tok-abc"]  # the bearer, not the raw header


def test_logout_reports_503_when_the_session_store_is_unreachable(client, monkeypatch):
    # The whole point: a logout that could not reach Redis has ended nothing.
    # It must NOT return a cheerful 200, or the caller clears its storage and
    # leaves a live session behind.
    def boom(_token):
        raise ConnectionError("redis down")

    monkeypatch.setattr(app_mod, "destroy_session", boom)

    resp = client.post("/logout", headers={"Authorization": "Bearer tok-abc"})

    assert resp.status_code == 503
    assert "retry" in resp.json()["detail"].lower()


def test_logout_without_a_token_is_not_an_error_but_reports_nothing_ended(client, monkeypatch):
    called = []
    monkeypatch.setattr(app_mod, "destroy_session", lambda t: called.append(t) or True)

    resp = client.post("/logout")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "session_ended": False}
    assert called == []  # nothing to destroy; don't pretend otherwise


def test_logout_does_not_leak_the_underlying_error_to_the_caller(client, monkeypatch):
    def boom(_token):
        raise ConnectionError("redis://user:secret@10.0.0.5:6379 refused")

    monkeypatch.setattr(app_mod, "destroy_session", boom)

    resp = client.post("/logout", headers={"Authorization": "Bearer tok-abc"})

    assert resp.status_code == 503
    assert "secret" not in resp.text
    assert "10.0.0.5" not in resp.text
