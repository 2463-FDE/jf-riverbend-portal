"""Unit tests for the MFA additions to services/gateway/security.py — the
Redis-backed login challenge and the generic rate limiter. Same _FakeRedis
monkeypatching pattern as tests/test_gateway_security.py's session tests.
"""
from conftest import load_module

security = load_module("services/gateway/security.py", "gateway_security_mfa")


class _FakeRedis:
    def __init__(self, store=None, counters=None):
        self._store = store or {}
        self._counters = counters or {}
        self._ttls = {}
        self.deleted = []

    def hset(self, key, mapping=None):
        self._store[key] = dict(mapping)

    def hgetall(self, key):
        return dict(self._store.get(key, {}))

    def expire(self, key, ttl):
        self._ttls[key] = ttl

    def delete(self, key):
        self.deleted.append(key)
        self._store.pop(key, None)
        self._counters.pop(key, None)

    def incr(self, key):
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]


# --- MFA login challenge -----------------------------------------------------


def test_create_mfa_challenge_carries_user_id_and_purpose_and_sets_ttl(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    token = security.create_mfa_challenge(7, purpose="login")

    assert token
    key = f"mfa_challenge:{token}"
    assert fake._store[key] == {"user_id": "7", "purpose": "login"}
    assert fake._ttls[key] == security.settings.mfa_challenge_timeout_seconds


def test_get_mfa_challenge_returns_user_id_and_purpose(monkeypatch):
    fake = _FakeRedis(store={"mfa_challenge:tok": {"user_id": "7", "purpose": "login"}})
    monkeypatch.setattr(security, "_redis", lambda: fake)

    data = security.get_mfa_challenge("tok")

    assert data == {"user_id": 7, "purpose": "login"}


def test_get_mfa_challenge_is_none_for_missing_or_empty_token(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    assert security.get_mfa_challenge("") is None
    assert security.get_mfa_challenge("nope") is None


def test_get_mfa_challenge_never_refreshes_its_own_ttl(monkeypatch):
    # Unlike a session, a challenge does NOT slide — reading it twice must
    # not extend its life, since it's meant to be single-purpose and short.
    fake = _FakeRedis(store={"mfa_challenge:tok": {"user_id": "7", "purpose": "login"}})
    monkeypatch.setattr(security, "_redis", lambda: fake)

    security.get_mfa_challenge("tok")
    security.get_mfa_challenge("tok")

    assert fake._ttls == {}


def test_destroy_mfa_challenge_deletes_the_key(monkeypatch):
    fake = _FakeRedis(store={"mfa_challenge:tok": {"user_id": "7"}})
    monkeypatch.setattr(security, "_redis", lambda: fake)

    security.destroy_mfa_challenge("tok")

    assert "mfa_challenge:tok" in fake.deleted
    assert "mfa_challenge:tok" not in fake._store


def test_destroy_mfa_challenge_is_a_noop_for_an_empty_token(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    security.destroy_mfa_challenge("")  # must not raise

    assert fake.deleted == []


# --- rate limiting ------------------------------------------------------------


def test_rate_limit_allows_up_to_the_limit(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    results = [security.rate_limit("k", limit=3, window_seconds=60) for _ in range(3)]

    assert results == [True, True, True]


def test_rate_limit_rejects_once_the_limit_is_exceeded(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    for _ in range(3):
        security.rate_limit("k", limit=3, window_seconds=60)
    fourth = security.rate_limit("k", limit=3, window_seconds=60)

    assert fourth is False


def test_rate_limit_sets_a_ttl_only_on_the_first_hit(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    security.rate_limit("k", limit=5, window_seconds=60)
    security.rate_limit("k", limit=5, window_seconds=60)

    assert fake._ttls == {"ratelimit:k": 60}  # EXPIRE called exactly once


def test_rate_limit_keys_are_independent(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    for _ in range(3):
        security.rate_limit("a", limit=3, window_seconds=60)

    # A different key starts its own fresh window.
    assert security.rate_limit("b", limit=3, window_seconds=60) is True
