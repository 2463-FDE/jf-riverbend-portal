"""Tests for the bounded async payer client (services/eligibility-service/payer_client.py).

No real network call is ever made — the httpx transport is a MockTransport,
and the retry `sleep` is a no-op that records requested delays instead of
actually waiting, so these tests run instantly. Coroutines are driven with
plain `asyncio.run()` rather than a pytest-asyncio plugin (not a project
dependency — see requirements-dev.txt) to avoid adding one just for this.
"""
import asyncio

import httpx
import pytest

from conftest import load_module

payer_client_mod = load_module("services/eligibility-service/payer_client.py", "eligibility_payer_client")

PayerClient = payer_client_mod.PayerClient
PayerTimeoutError = payer_client_mod.PayerTimeoutError
RetriesExhaustedError = payer_client_mod.RetriesExhaustedError


def _client(handler, *, max_retries=2, sleep=None, rand=None):
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    return (
        PayerClient(
            base_url="https://payer.example.test/eligibility",
            api_key="test-key",
            timeout_seconds=1.0,
            max_retries=max_retries,
            transport=httpx.MockTransport(handler),
            sleep=sleep or _sleep,
            rand=rand or (lambda lo, hi: hi),  # deterministic: always the cap
        ),
        sleeps,
    )


# --- successful completion ---------------------------------------------------


def test_success_maps_2xx_to_active():
    def handler(request):
        assert request.url.params["member_id"] == "BCBS4471"
        return httpx.Response(200, json={"ok": True})

    client, sleeps = _client(handler)

    result = asyncio.run(client.check("BCBS4471"))

    assert result == {"insurance_id": "BCBS4471", "active": True, "raw_status": 200}
    assert sleeps == []


def test_success_maps_non_2xx_to_inactive_not_an_error():
    def handler(request):
        return httpx.Response(404)

    client, sleeps = _client(handler)

    result = asyncio.run(client.check("UNKNOWN1"))

    assert result["active"] is False
    assert result["raw_status"] == 404
    assert sleeps == []  # a plain non-2xx response is not retried


def test_authorization_header_and_params_sent():
    captured = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200)

    client, _ = _client(handler)
    asyncio.run(client.check("MEM1"))

    assert captured["headers"]["authorization"] == "Bearer test-key"
    assert captured["params"] == {"member_id": "MEM1", "service_type": "30"}


# --- retry / backoff on transport failure -------------------------------------


def test_timeout_is_retried_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectTimeout("boom")
        return httpx.Response(200)

    client, sleeps = _client(handler, max_retries=2)

    result = asyncio.run(client.check("MEM1"))

    assert result["active"] is True
    assert attempts["n"] == 2
    assert len(sleeps) == 1  # one backoff between attempt 1 and attempt 2


def test_transient_transport_error_is_retried_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200)

    client, sleeps = _client(handler, max_retries=2)

    result = asyncio.run(client.check("MEM1"))

    assert result["active"] is True
    assert len(sleeps) == 1


def test_retries_exhausted_raises_and_stops_at_configured_max():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ConnectTimeout("still down")

    client, sleeps = _client(handler, max_retries=2)

    with pytest.raises(RetriesExhaustedError):
        asyncio.run(client.check("MEM1"))

    assert attempts["n"] == 3  # initial attempt + 2 retries
    assert len(sleeps) == 2  # no sleep scheduled after the final failed attempt


def test_retries_exhausted_error_chains_the_last_transport_error():
    def handler(request):
        raise httpx.ReadTimeout("slow payer")

    client, _ = _client(handler, max_retries=0)

    with pytest.raises(RetriesExhaustedError) as excinfo:
        asyncio.run(client.check("MEM1"))

    assert isinstance(excinfo.value.__cause__, PayerTimeoutError)


def test_non_transport_exception_is_not_caught():
    def handler(request):
        raise ValueError("some unrelated bug")

    client, sleeps = _client(handler, max_retries=3)

    with pytest.raises(ValueError):
        asyncio.run(client.check("MEM1"))

    assert sleeps == []  # never classified as retryable, so never retried


# --- backoff delay shape -------------------------------------------------------


def test_backoff_delay_grows_with_attempt_number_and_is_capped():
    client = PayerClient(base_url="https://x.test", api_key="k")

    delays_by_attempt = [client._backoff_delay(0) for _ in range(200)]
    later_delays = [client._backoff_delay(4) for _ in range(200)]

    assert max(delays_by_attempt) <= 0.5
    assert max(later_delays) <= 8.0
    assert max(later_delays) > max(delays_by_attempt)
