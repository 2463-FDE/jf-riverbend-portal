"""Tests for visit-scoped structured memory (libs/eligibility_agent/memory.py):
TTL, key isolation from other Redis uses in the stack, and the best-effort
degrade-on-failure behavior that mirrors the Stage 1 Hardening Fix applied to
services/eligibility-service/cache.py — a memory-store outage must degrade to
"no memory for this visit", never an unhandled exception.
"""
import logging
from datetime import datetime, timezone

from libs.eligibility_agent.contracts import VisitContext
from libs.eligibility_agent.memory import KEY_PREFIX, RedisVisitMemory


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.set_calls = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.set_calls.append({"key": key, "value": value, "ex": ex})
        self.store[key] = value


class _RaisingRedis:
    def get(self, key):
        raise ConnectionError("redis is down")

    def set(self, key, value, ex=None):
        raise ConnectionError("redis is down")


def _context(visit_id="visit-1"):
    return VisitContext(visit_id=visit_id, insurance_id="BCBS1", updated_at=datetime.now(timezone.utc))


def test_round_trip_get_put():
    memory = RedisVisitMemory(_FakeRedis())

    memory.put(_context())
    result = memory.get("visit-1")

    assert result.visit_id == "visit-1"
    assert result.insurance_id == "BCBS1"


def test_missing_visit_returns_none():
    memory = RedisVisitMemory(_FakeRedis())

    assert memory.get("never-seen") is None


def test_key_uses_a_dedicated_prefix_not_shared_with_sessions_or_the_eligibility_cache():
    redis = _FakeRedis()
    memory = RedisVisitMemory(redis)

    memory.put(_context("visit-1"))

    key = redis.set_calls[0]["key"]
    assert key == f"{KEY_PREFIX}visit-1"
    assert not key.startswith("session:")  # gateway login sessions
    assert not key.startswith("elig:lkg:")  # Stage 1 last-known-good eligibility cache


def test_put_passes_the_configured_ttl():
    redis = _FakeRedis()
    memory = RedisVisitMemory(redis, ttl_seconds=900)

    memory.put(_context())

    assert redis.set_calls[0]["ex"] == 900


def test_two_visits_do_not_share_a_key():
    redis = _FakeRedis()
    memory = RedisVisitMemory(redis)

    memory.put(_context("visit-A"))
    memory.put(_context("visit-B"))

    assert memory.get("visit-A").visit_id == "visit-A"
    assert memory.get("visit-B").visit_id == "visit-B"


def test_get_failure_degrades_to_none_instead_of_raising(caplog):
    memory = RedisVisitMemory(_RaisingRedis())

    with caplog.at_level(logging.WARNING):
        result = memory.get("visit-1")

    assert result is None
    assert caplog.records
    for record in caplog.records:
        assert "redis is down" not in record.getMessage()


def test_put_failure_is_swallowed_not_raised(caplog):
    memory = RedisVisitMemory(_RaisingRedis())

    with caplog.at_level(logging.WARNING):
        memory.put(_context())  # must not raise

    assert caplog.records
    for record in caplog.records:
        assert "redis is down" not in record.getMessage()
