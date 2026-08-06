"""services/intake-service/app.py::healthz — Round-13 review (2026-08-06, PR
#20). Same fix as gateway's and records-service's /healthz: a missing or
placeholder INTERNAL_SERVICE_TOKEN must fail the healthcheck instead of
letting the container report healthy while every /intake call 401s.
"""
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/intake-service/app.py", "intake_app_healthz")

VALID_TOKEN = "valid-internal-token-well-over-the-32-char-floor"


def _client():
    return TestClient(app_mod.app)


def test_healthz_ok_when_internal_token_configured(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", VALID_TOKEN)

    resp = _client().get("/healthz")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_healthz_fails_when_internal_token_unset(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "")

    resp = _client().get("/healthz")

    assert resp.status_code == 503


def test_healthz_fails_when_internal_token_is_a_short_placeholder(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "changeme")

    resp = _client().get("/healthz")

    assert resp.status_code == 503
