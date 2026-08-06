"""services/records-service/app.py::healthz — Round-13 review (2026-08-06,
PR #20). Same fix as gateway's and intake-service's /healthz: a missing or
placeholder INTERNAL_SERVICE_TOKEN must fail the healthcheck instead of
letting the container report healthy while every gateway-forwarded call
401s.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_healthz")

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


# --- Round-17 review (2026-08-06): fail at process startup too, not just at
# /healthz — see test_gateway_healthz.py's identical section for the full
# rationale. Never fires under TestClient's no-`with`-block usage above, so
# tested directly here.


def test_lifespan_raises_when_internal_token_missing(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "")

    async def _run():
        async with app_mod.lifespan(app_mod.app):
            pass

    with pytest.raises(RuntimeError, match="INTERNAL_SERVICE_TOKEN"):
        asyncio.run(_run())


def test_lifespan_succeeds_when_internal_token_configured(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", VALID_TOKEN)

    async def _run():
        async with app_mod.lifespan(app_mod.app):
            pass

    asyncio.run(_run())  # must not raise
