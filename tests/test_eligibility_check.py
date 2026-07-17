"""Unit tests for eligibility check orchestration (eligibility-service/check.py).

Stage 1 resilience fix: this replaces the previous test file, which asserted
the OLD (buggy) behavior indirectly by only covering the raw payer call with
no timeout/retry/breaker — that gap was deliberate and is exactly what this
stage adds. See tests/test_eligibility_payer_client.py, test_eligibility_breaker.py,
and test_eligibility_cache.py for the underlying components; this file covers
how check() orchestrates them, in particular that a transport failure maps to
`unknown` (or `stale`, if a last-known-good cache entry exists) and never
silently becomes `inactive`.
"""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from conftest import load_module

check_mod = load_module("services/eligibility-service/check.py", "eligibility_check")

PayerClient = check_mod.PayerClient
CircuitBreaker = check_mod.CircuitBreaker
LastKnownGoodCache = check_mod.LastKnownGoodCache
EligibilityStatus = check_mod.EligibilityStatus
RetriesExhaustedError = check_mod.RetriesExhaustedError
check = check_mod.check

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


class _StubPayerClient:
    """Stands in for PayerClient — a script of results/exceptions, one per call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def check(self, insurance_id):
        self.calls.append(insurance_id)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _env(*, script, breaker=None, cache=None):
    client = _StubPayerClient(script)
    breaker = breaker or CircuitBreaker(failure_threshold=3, reset_timeout_seconds=30)
    cache = cache if cache is not None else LastKnownGoodCache(_FakeRedis(), now=lambda: NOW)
    return client, breaker, cache


class _RaisingRedis:
    """Stands in for a Redis client that's completely unreachable — every call
    raises. Used to prove check()'s cache-boundary is best-effort end-to-end
    (cache.py itself is unit-tested for this in test_eligibility_cache.py)."""

    class _Down(Exception):
        pass

    def get(self, key):
        raise self._Down("connection refused")

    def set(self, key, value, ex=None):
        raise self._Down("connection refused")


# --- happy path -----------------------------------------------------------


def test_active_result():
    client, breaker, cache = _env(script=[{"insurance_id": "MEM1", "active": True, "raw_status": 200}])

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert result.status == EligibilityStatus.ACTIVE
    assert result.error_type is None
    assert result.checked_at == NOW


def test_inactive_result():
    client, breaker, cache = _env(script=[{"insurance_id": "MEM1", "active": False, "raw_status": 404}])

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert result.status == EligibilityStatus.INACTIVE
    assert result.error_type is None


def test_successful_check_is_written_to_cache():
    fake_redis = _FakeRedis()
    cache = LastKnownGoodCache(fake_redis, now=lambda: NOW)
    client, breaker, _ = _env(script=[{"insurance_id": "MEM1", "active": True, "raw_status": 200}])

    asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert fake_redis.store  # last-known-good was persisted


def test_successful_check_resets_the_breaker():
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=30)
    breaker.record_failure()
    breaker.record_failure()
    client, _, cache = _env(script=[{"insurance_id": "MEM1", "active": True, "raw_status": 200}])

    asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow_request() is True  # only 2 consecutive since the reset


# --- the bug fix: transport failure never becomes "inactive" --------------


def test_transport_failure_with_no_cache_maps_to_unknown_not_inactive():
    client, breaker, cache = _env(script=[RetriesExhaustedError("PayerTimeoutError")])

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert result.status == EligibilityStatus.UNKNOWN
    assert result.status != EligibilityStatus.INACTIVE
    assert result.error_type == "RetriesExhaustedError"


def test_transport_failure_records_a_breaker_failure():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=30)
    client, _, cache = _env(script=[RetriesExhaustedError("boom")], breaker=breaker)

    asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert breaker.allow_request() is False  # threshold=1, tripped immediately


def test_transport_failure_with_cached_entry_falls_back_to_stale():
    fake_redis = _FakeRedis()
    write_cache = LastKnownGoodCache(fake_redis, fresh_ttl_seconds=60, now=lambda: NOW)
    write_cache.set(
        check_mod.EligibilityResult(insurance_id="MEM1", status=EligibilityStatus.ACTIVE, checked_at=NOW)
    )

    later = datetime(2026, 7, 17, 12, 30, 0, tzinfo=timezone.utc)  # 30 min later, past fresh_ttl
    read_cache = LastKnownGoodCache(fake_redis, fresh_ttl_seconds=60, now=lambda: later)
    client, breaker, _ = _env(script=[RetriesExhaustedError("PayerTimeoutError")])

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=read_cache, now=lambda: later))

    assert result.status == EligibilityStatus.STALE
    assert result.cached_status == EligibilityStatus.ACTIVE
    assert result.error_type == "RetriesExhaustedError"


def test_open_circuit_short_circuits_without_calling_the_client():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=30)
    breaker.record_failure()  # already open
    client, _, cache = _env(script=[], breaker=breaker)

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert client.calls == []  # never attempted while the breaker is open
    assert result.status == EligibilityStatus.UNKNOWN
    assert result.error_type == "CircuitOpenError"


def test_open_circuit_serves_stale_cache_when_available():
    fake_redis = _FakeRedis()
    write_cache = LastKnownGoodCache(fake_redis, fresh_ttl_seconds=60, now=lambda: NOW)
    write_cache.set(
        check_mod.EligibilityResult(insurance_id="MEM1", status=EligibilityStatus.INACTIVE, checked_at=NOW)
    )

    later = datetime(2026, 7, 17, 13, 0, 0, tzinfo=timezone.utc)
    read_cache = LastKnownGoodCache(fake_redis, fresh_ttl_seconds=60, now=lambda: later)
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=3600)
    breaker.record_failure()
    client, _, _ = _env(script=[])

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=read_cache, now=lambda: later))

    assert client.calls == []
    assert result.status == EligibilityStatus.STALE
    assert result.cached_status == EligibilityStatus.INACTIVE
    assert result.error_type == "CircuitOpenError"


# --- Stage 1 Hardening Fix: a transient HTTP response is never cached as
# "inactive" ------------------------------------------------------------------
# These use the REAL PayerClient (via httpx.MockTransport), not the stub, so
# the proof runs through the exact same retry-classification code check()
# relies on in production, not just a hand-scripted stand-in for it.


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_transient_http_status_exhausted_maps_to_unknown_and_is_never_cached(status_code):
    def handler(request):
        return httpx.Response(status_code)

    real_client = PayerClient(
        base_url="https://payer.example.test/eligibility",
        api_key="k",
        max_retries=1,
        transport=httpx.MockTransport(handler),
        sleep=lambda seconds: asyncio.sleep(0),
    )
    fake_redis = _FakeRedis()
    cache = LastKnownGoodCache(fake_redis, now=lambda: NOW)
    breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=30)

    result = asyncio.run(check("MEM1", client=real_client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert result.status == EligibilityStatus.UNKNOWN
    assert result.status != EligibilityStatus.INACTIVE
    assert result.error_type == "RetriesExhaustedError"
    assert fake_redis.store == {}  # nothing was ever cached for this failure


def test_transient_http_status_that_recovers_is_cached_as_its_real_answer():
    """The inverse check: once retries succeed, the real (now-terminal) answer
    IS cached normally — the hardening fix only changes what happens while
    the payer is degraded, not the happy path."""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(404)  # terminal business answer: inactive

    real_client = PayerClient(
        base_url="https://payer.example.test/eligibility",
        api_key="k",
        max_retries=2,
        transport=httpx.MockTransport(handler),
        sleep=lambda seconds: asyncio.sleep(0),
    )
    fake_redis = _FakeRedis()
    cache = LastKnownGoodCache(fake_redis, now=lambda: NOW)
    breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=30)

    result = asyncio.run(check("MEM1", client=real_client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert result.status == EligibilityStatus.INACTIVE
    assert fake_redis.store  # a genuine terminal answer is still cached


# --- Stage 1 Hardening Fix: cache failures are best-effort, end to end -------


def test_cache_set_failure_still_returns_the_live_payer_result():
    client, breaker, _ = _env(script=[{"insurance_id": "MEM1", "active": True, "raw_status": 200}])
    cache = LastKnownGoodCache(_RaisingRedis(), now=lambda: NOW)

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert result.status == EligibilityStatus.ACTIVE  # the live result, despite the cache write failing
    assert result.error_type is None


def test_cache_get_failure_during_fallback_degrades_to_unknown_not_an_exception():
    client, breaker, _ = _env(script=[RetriesExhaustedError("PayerTimeoutError")])
    cache = LastKnownGoodCache(_RaisingRedis(), now=lambda: NOW)

    result = asyncio.run(check("MEM1", client=client, breaker=breaker, cache=cache, now=lambda: NOW))

    assert result.status == EligibilityStatus.UNKNOWN  # cache-read failure treated as a miss
    assert result.error_type == "RetriesExhaustedError"
