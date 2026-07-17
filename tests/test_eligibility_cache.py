"""Tests for the Redis-backed last-known-good eligibility cache
(services/eligibility-service/cache.py). Uses a tiny in-memory fake standing
in for a real `redis.Redis` client — no live Redis, matching how the rest of
this suite fakes providers/transports rather than installing test infra.
"""
from datetime import datetime, timedelta, timezone

from conftest import load_module

cache_mod = load_module("services/eligibility-service/cache.py", "eligibility_cache")
contracts_mod = load_module("services/eligibility-service/contracts.py", "eligibility_contracts")

LastKnownGoodCache = cache_mod.LastKnownGoodCache
EligibilityResult = contracts_mod.EligibilityResult
EligibilityStatus = contracts_mod.EligibilityStatus


class FakeRedis:
    """Minimal get/set-with-ex double; no real TTL expiry — tests simulate an
    expired entry by simply not seeding it, which is what a real Redis key
    past its `ex` looks like from the cache's point of view."""

    def __init__(self):
        self.store = {}
        self.set_calls = []

    def set(self, key, value, ex=None):
        self.set_calls.append({"key": key, "value": value, "ex": ex})
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


class RaisingRedis:
    """Stands in for a Redis client that's completely unreachable — both
    get() and set() raise. Used to prove the cache is best-effort."""

    class Down(Exception):
        pass

    def get(self, key):
        raise self.Down("connection refused")

    def set(self, key, value, ex=None):
        raise self.Down("connection refused")


class RaisingOnlyOnGetRedis(FakeRedis):
    """A cache that can be written to normally but whose reads are broken —
    e.g. a malformed/corrupted entry, or a read-path-specific outage."""

    def get(self, key):
        raise RuntimeError("corrupted read")


BASE_TIME = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _cache(redis=None, *, fresh_ttl=300, stale_ttl=3600, now=BASE_TIME):
    return LastKnownGoodCache(
        redis or FakeRedis(),
        fresh_ttl_seconds=fresh_ttl,
        stale_ttl_seconds=stale_ttl,
        now=lambda: now,
    )


def _result(status, checked_at=BASE_TIME, **kwargs):
    return EligibilityResult(insurance_id="MEM1", status=status, checked_at=checked_at, **kwargs)


# --- what gets cached ----------------------------------------------------


def test_active_result_is_cached():
    redis = FakeRedis()
    cache = _cache(redis)

    cache.set(_result(EligibilityStatus.ACTIVE))

    assert redis.store  # something was written
    assert redis.set_calls[0]["key"] == "elig:lkg:MEM1"
    assert redis.set_calls[0]["ex"] == 3600


def test_inactive_result_is_cached():
    redis = FakeRedis()
    cache = _cache(redis)

    cache.set(_result(EligibilityStatus.INACTIVE))

    assert redis.store


def test_unknown_result_is_never_cached():
    redis = FakeRedis()
    cache = _cache(redis)

    cache.set(_result(EligibilityStatus.UNKNOWN))

    assert redis.store == {}


def test_stale_result_is_never_cached():
    redis = FakeRedis()
    cache = _cache(redis)

    cache.set(_result(EligibilityStatus.STALE))

    assert redis.store == {}


def test_pending_result_is_never_cached():
    redis = FakeRedis()
    cache = _cache(redis)

    cache.set(_result(EligibilityStatus.PENDING))

    assert redis.store == {}


# --- reads: fresh / stale / missing ---------------------------------------


def test_missing_entry_returns_none():
    cache = _cache()

    assert cache.get("NEVER-CHECKED") is None


def test_fresh_hit_returns_original_status_unmodified():
    redis = FakeRedis()
    cache = _cache(redis, fresh_ttl=300, now=BASE_TIME)
    cache.set(_result(EligibilityStatus.ACTIVE, checked_at=BASE_TIME))

    # read 60s later, still within the 300s fresh window
    read_cache = _cache(redis, fresh_ttl=300, now=BASE_TIME + timedelta(seconds=60))
    result = read_cache.get("MEM1")

    assert result.status == EligibilityStatus.ACTIVE
    assert result.cached_status is None
    assert result.stale_age_seconds is None


def test_past_fresh_ttl_returns_stale_with_original_status_and_age():
    redis = FakeRedis()
    cache = _cache(redis, fresh_ttl=300, stale_ttl=3600, now=BASE_TIME)
    cache.set(_result(EligibilityStatus.ACTIVE, checked_at=BASE_TIME))

    read_cache = _cache(redis, fresh_ttl=300, stale_ttl=3600, now=BASE_TIME + timedelta(seconds=600))
    result = read_cache.get("MEM1")

    assert result.status == EligibilityStatus.STALE
    assert result.cached_status == EligibilityStatus.ACTIVE
    assert result.stale_age_seconds == 600


def test_never_shows_stale_as_current():
    """A cache hit past the fresh window must never be indistinguishable from
    a live, current answer — this is the exact property the front-end status
    surface (Stage 3) depends on."""
    redis = FakeRedis()
    cache = _cache(redis, fresh_ttl=60, now=BASE_TIME)
    cache.set(_result(EligibilityStatus.INACTIVE, checked_at=BASE_TIME))

    read_cache = _cache(redis, fresh_ttl=60, now=BASE_TIME + timedelta(seconds=61))
    result = read_cache.get("MEM1")

    assert result.status != EligibilityStatus.INACTIVE
    assert result.status == EligibilityStatus.STALE


def test_entry_expired_out_of_redis_returns_none():
    # Simulates a key past stale_ttl: Redis' own TTL would have evicted it,
    # so from the cache's point of view it's simply not present.
    cache = _cache(FakeRedis())

    assert cache.get("MEM1") is None


def test_rejects_stale_ttl_shorter_than_fresh_ttl():
    import pytest

    with pytest.raises(ValueError):
        _cache(fresh_ttl=600, stale_ttl=300)


# --- Stage 1 Hardening Fix: get/set are best-effort -----------------------


def test_set_swallows_a_redis_failure_instead_of_raising():
    cache = _cache(RaisingRedis())

    cache.set(_result(EligibilityStatus.ACTIVE))  # must not raise


def test_set_logs_the_error_type_only_on_a_redis_failure(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger=cache_mod.__name__)
    cache = _cache(RaisingRedis())

    cache.set(_result(EligibilityStatus.ACTIVE))

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "error_type=Down" in text
    assert "connection refused" not in text  # raw exception message never logged


def test_get_returns_none_on_a_redis_failure_instead_of_raising():
    cache = _cache(RaisingRedis())

    assert cache.get("MEM1") is None


def test_get_returns_none_on_a_malformed_cached_entry_instead_of_raising():
    cache = _cache(RaisingOnlyOnGetRedis())

    assert cache.get("MEM1") is None


def test_get_failure_log_contains_no_raw_exception_text(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger=cache_mod.__name__)
    cache = _cache(RaisingRedis())

    cache.get("MEM1-SECRET")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "MEM1-SECRET" not in text
    assert "connection refused" not in text
