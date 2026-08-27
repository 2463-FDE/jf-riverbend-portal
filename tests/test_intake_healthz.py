"""services/intake-service/app.py::healthz — Round-13 review (2026-08-06, PR
#20). Same fix as gateway's and records-service's /healthz: a missing or
placeholder INTERNAL_SERVICE_TOKEN must fail the healthcheck instead of
letting the container report healthy while every /intake call 401s.
"""
import asyncio
import base64
import os
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/intake-service/app.py", "intake_app_healthz")
# app.py's `from phi import ...` resolves through sys.modules — this is the
# SAME module object app_mod's encrypt/decrypt/get_key_provider calls use,
# not a separate copy. Patching attributes on it (not on app_mod) is what
# actually affects lifespan's PHI key validation.
phi_mod = sys.modules["phi"]

VALID_TOKEN = "valid-internal-token-well-over-the-32-char-floor"

# w8-planner-2 P2 (adr/0012): lifespan now also validates PHI keys (see the
# "Round-17 review" section below). get_key_provider() caches its
# EnvKeyProvider at the phi module level — that cache can persist across
# test FILES within one pytest process (conftest.load_module only evicts a
# stale sys.modules["phi"] pointing at a DIFFERENT service's phi.py, not a
# same-service one loaded by an earlier test file). Every test here
# explicitly overrides phi_mod._key_provider via monkeypatch rather than
# relying on env vars alone, so each test's intent holds regardless of
# what an earlier test (in this file or another) already constructed.
_VALID_PHI_PROVIDER = phi_mod.EnvKeyProvider(
    {
        "PHI_ACTIVE_KEY_VERSION": "v1",
        "PHI_ENCRYPTION_KEY_V1": base64.b64encode(os.urandom(32)).decode(),
        "PHI_BLIND_INDEX_KEY_V1": base64.b64encode(os.urandom(32)).decode(),
    }
)


def _set_valid_phi_provider(monkeypatch):
    monkeypatch.setattr(phi_mod, "_key_provider", _VALID_PHI_PROVIDER)


def _clear_phi_provider_and_env(monkeypatch):
    """Forces the NEXT get_key_provider() call to reconstruct from
    whatever env vars the test itself sets — used by the failure tests
    below, which need EnvKeyProvider's own validation to actually run."""
    monkeypatch.setattr(phi_mod, "_key_provider", None)
    for var in ("PHI_ACTIVE_KEY_VERSION", "PHI_ENCRYPTION_KEY_V1", "PHI_BLIND_INDEX_KEY_V1"):
        monkeypatch.delenv(var, raising=False)


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
    _set_valid_phi_provider(monkeypatch)

    async def _run():
        async with app_mod.lifespan(app_mod.app):
            pass

    asyncio.run(_run())  # must not raise


# --- w8-planner-2 P2 (adr/0012): lifespan also refuses to start on invalid
# PHI key configuration — same "process startup, not just /healthz" reasoning
# as the INTERNAL_SERVICE_TOKEN checks above.


def test_lifespan_raises_when_phi_keys_missing(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", VALID_TOKEN)
    _clear_phi_provider_and_env(monkeypatch)

    async def _run():
        async with app_mod.lifespan(app_mod.app):
            pass

    with pytest.raises(RuntimeError, match="PHI key configuration"):
        asyncio.run(_run())


def test_lifespan_raises_when_phi_encryption_and_blind_index_keys_are_identical(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", VALID_TOKEN)
    _clear_phi_provider_and_env(monkeypatch)
    same_key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("PHI_ACTIVE_KEY_VERSION", "v1")
    monkeypatch.setenv("PHI_ENCRYPTION_KEY_V1", same_key)
    monkeypatch.setenv("PHI_BLIND_INDEX_KEY_V1", same_key)

    async def _run():
        async with app_mod.lifespan(app_mod.app):
            pass

    with pytest.raises(RuntimeError, match="PHI key configuration"):
        asyncio.run(_run())
