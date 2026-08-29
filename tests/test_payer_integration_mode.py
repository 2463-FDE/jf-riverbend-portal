"""W10 Final Stage 1 — services/eligibility-service/payer_mode.py, and its
enforcement in check.py::check.

Replaces the old implicit rule ("PAYER_API_KEY blank => simulate") with an
explicit PAYER_INTEGRATION_MODE. Covers: simulation makes zero client/
breaker/cache calls ever; live mode with a real credential+endpoint runs the
normal resilience path; live mode with a missing/placeholder credential or
endpoint is rejected before any client call.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from conftest import load_module

payer_mode = load_module("services/eligibility-service/payer_mode.py", "payer_mode")
check_mod = load_module("services/eligibility-service/check.py", "eligibility_check_mode")

check = check_mod.check
CircuitBreaker = check_mod.CircuitBreaker
LastKnownGoodCache = check_mod.LastKnownGoodCache
EligibilityStatus = check_mod.EligibilityStatus

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


class _CountingClient:
    def __init__(self):
        self.calls = 0

    async def check(self, insurance_id):
        self.calls += 1
        return {"insurance_id": insurance_id, "active": True, "raw_status": 200}


def _env():
    client = _CountingClient()
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=30)
    cache = LastKnownGoodCache(_FakeRedis(), now=lambda: NOW)
    return client, breaker, cache


# --- payer_mode.validate / config_error ------------------------------------


def test_simulation_never_needs_credentials():
    payer_mode.validate("simulation", api_key="", api_url="")  # must not raise


def test_live_requires_an_api_key():
    with pytest.raises(payer_mode.PayerModeConfigError, match="PAYER_API_KEY"):
        payer_mode.validate("live", api_key="", api_url="https://real-payer.example/v1")


def test_live_rejects_the_shipped_placeholder_url():
    with pytest.raises(payer_mode.PayerModeConfigError, match="PAYER_API_URL"):
        payer_mode.validate("live", api_key="a-real-key", api_url="https://edi.example.com/v1/eligibility")


def test_live_with_real_key_and_url_is_valid():
    payer_mode.validate("live", api_key="a-real-key", api_url="https://real-payer.example/v1")


def test_an_unrecognized_mode_is_rejected():
    with pytest.raises(payer_mode.PayerModeConfigError, match="simulation.*live|live.*simulation"):
        payer_mode.validate("auto", api_key="k", api_url="https://real-payer.example/v1")


def test_config_error_returns_a_message_instead_of_raising():
    assert payer_mode.config_error("simulation", api_key="", api_url="") is None
    assert "PAYER_API_KEY" in payer_mode.config_error("live", api_key="", api_url="x")


# --- check.py enforcement ---------------------------------------------------


def test_simulation_mode_makes_zero_client_calls(monkeypatch):
    monkeypatch.setattr(check_mod.settings, "payer_integration_mode", "simulation")
    client, breaker, cache = _env()

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert client.calls == 0
    assert result.status == EligibilityStatus.UNKNOWN
    assert result.error_type == "SimulationMode"


def test_live_mode_with_real_config_reaches_the_client(monkeypatch):
    monkeypatch.setattr(check_mod.settings, "payer_integration_mode", "live")
    monkeypatch.setattr(check_mod.settings, "payer_api_key", "a-real-key")
    monkeypatch.setattr(check_mod.settings, "payer_api_url", "https://real-payer.example/v1")
    client, breaker, cache = _env()

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert client.calls == 1
    assert result.status == EligibilityStatus.ACTIVE


def test_live_mode_with_placeholder_config_never_calls_the_client(monkeypatch):
    monkeypatch.setattr(check_mod.settings, "payer_integration_mode", "live")
    monkeypatch.setattr(check_mod.settings, "payer_api_key", "")  # never set
    monkeypatch.setattr(check_mod.settings, "payer_api_url", "https://edi.example.com/v1/eligibility")
    client, breaker, cache = _env()

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert client.calls == 0
    assert result.status == EligibilityStatus.UNKNOWN
    assert result.error_type == "PayerModeConfigError"


def test_app_startup_refuses_live_mode_with_a_placeholder(monkeypatch):
    app_mod = load_module("services/eligibility-service/app.py", "eligibility_app_payer_mode")
    monkeypatch.setattr(app_mod.settings, "internal_service_token",
                         "test-internal-token-abc123-well-over-the-32-char-floor")
    monkeypatch.setattr(app_mod.settings, "payer_integration_mode", "live")
    monkeypatch.setattr(app_mod.settings, "payer_api_key", "")

    with pytest.raises(RuntimeError, match="PAYER_API_KEY"):
        app_mod._fail_fast_on_an_unusable_payer_mode()
