"""services/gateway/app.py::healthz — Round-13 review (2026-08-06, PR #20).

INTERNAL_SERVICE_TOKEN defaults to an empty string and used to only be
checked per-request (proxy_intake/proxy_patient_view), so a deployment with
it unset would start and pass docker-compose's healthcheck (which just hits
/healthz) while every gateway-forwarded intake/patient-view call fails
closed with 401 downstream — a healthy-looking outage. /healthz now fails
the same presence/length check before reporting "ok".
"""
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_healthz")

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
    # e.g. "changeme" — same floor _MIN_INTERNAL_TOKEN_LENGTH enforces
    # downstream on every real request.
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "changeme")

    resp = _client().get("/healthz")

    assert resp.status_code == 503
