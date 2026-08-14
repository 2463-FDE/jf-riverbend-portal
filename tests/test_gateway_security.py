"""Unit tests for gateway password hashing/verification (no DB, no Redis I/O)."""
import pytest

from conftest import load_module

security = load_module("services/gateway/security.py", "gateway_security")


def test_hash_then_verify_roundtrip():
    encoded = security.hash_password("portal123")
    assert encoded.startswith("pbkdf2_sha256$")
    assert security.verify_password("portal123", encoded) is True


def test_verify_rejects_wrong_password():
    encoded = security.hash_password("portal123")
    assert security.verify_password("nope", encoded) is False


def test_verify_rejects_malformed_hash():
    assert security.verify_password("x", "not-a-valid-hash") is False
    assert security.verify_password("x", "") is False


def test_salt_is_random_per_hash():
    a = security.hash_password("same")
    b = security.hash_password("same")
    assert a != b  # different salts
    assert security.verify_password("same", a)
    assert security.verify_password("same", b)


def test_verifies_seeded_fixture_hash():
    # The exact hash the seed generator produces for the demo accounts.
    salt = "riverbend02saltval0"
    encoded = security.hash_password("portal123", salt=salt)
    assert security.verify_password("portal123", encoded)


# --- session principal + TTL (PR #23 review round 2, finding 3b) -------------


class _FakeRedis:
    def __init__(self, store=None):
        self.hset_calls = []
        self.expire_calls = []
        self.deleted = []
        self._store = store or {}

    def hset(self, key, mapping=None):
        self.hset_calls.append((key, mapping))
        self._store[key] = dict(mapping)

    def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))

    def hgetall(self, key):
        return dict(self._store.get(key, {}))

    def delete(self, key):
        self.deleted.append(key)


def test_create_session_carries_user_id_and_sets_ttl(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)
    monkeypatch.setattr(security, "_now", lambda: 1000.0)

    token = security.create_session(2, "frontdesk", "staff")

    assert token
    key, mapping = fake.hset_calls[0]
    assert key == f"session:{token}"
    # The stable users.id is the principal; username/role are metadata;
    # created_at anchors the absolute-lifetime cap checked in get_session.
    assert mapping == {"user_id": "2", "username": "frontdesk", "role": "staff", "created_at": "1000.0"}
    # Sessions no longer live forever — a TTL is set at creation.
    assert fake.expire_calls == [(f"session:{token}", security.settings.session_timeout_seconds)]


def test_get_session_refreshes_ttl_on_read(monkeypatch):
    key = "session:tok"
    fake = _FakeRedis(store={key: {"user_id": "2", "username": "frontdesk", "role": "staff", "created_at": "1000.0"}})
    monkeypatch.setattr(security, "_redis", lambda: fake)
    monkeypatch.setattr(security, "_now", lambda: 1010.0)  # well inside both TTLs

    data = security.get_session("tok")

    assert data["user_id"] == "2"
    # Sliding idle timeout: an active session's TTL is refreshed on each read.
    assert fake.expire_calls == [(key, security.settings.session_timeout_seconds)]


def test_get_session_is_none_for_missing_token_and_does_not_refresh(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    assert security.get_session("") is None
    assert security.get_session("nope") is None
    assert fake.expire_calls == []  # nothing to refresh for a nonexistent session


def test_get_session_rejects_and_deletes_a_legacy_session_without_user_id(monkeypatch):
    # PR #23 review round 3: pre-user_id sessions (only username/role, never
    # expiring) can't authorize anything — must be cleared, not accepted.
    key = "session:legacy"
    fake = _FakeRedis(store={key: {"username": "frontdesk", "role": "staff"}})
    monkeypatch.setattr(security, "_redis", lambda: fake)

    assert security.get_session("legacy") is None
    assert key in fake.deleted        # cleared, forcing a clean re-login
    assert fake.expire_calls == []    # a malformed session's TTL is never extended


# --- destroy_session contract (shared-workstation fix) ---------------------


def test_destroy_session_deletes_the_key_and_reports_success(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    assert security.destroy_session("tok") is True
    assert fake.deleted == ["session:tok"]


def test_destroy_session_reports_failure_for_an_empty_token(monkeypatch):
    # No key to delete, and the caller may still hold a valid token it didn't
    # send — we cannot claim a session was ended.
    fake = _FakeRedis()
    monkeypatch.setattr(security, "_redis", lambda: fake)

    assert security.destroy_session("") is False
    assert fake.deleted == []


def test_destroy_session_propagates_a_store_failure_rather_than_swallowing_it(monkeypatch):
    # A logout that cannot reach Redis has not ended anything. Swallowing this
    # is what let the UI show a signed-out screen over a live session.
    class _Broken:
        def delete(self, _key):
            raise ConnectionError("redis down")

    monkeypatch.setattr(security, "_redis", lambda: _Broken())

    with pytest.raises(ConnectionError):
        security.destroy_session("tok")


# --- absolute session lifetime cap -----------------------------------------


def test_get_session_rejects_and_deletes_a_session_missing_created_at(monkeypatch):
    # A session issued before this fix has no created_at and can't be aged —
    # same treatment as the missing-user_id legacy case above.
    key = "session:pre-fix"
    fake = _FakeRedis(store={key: {"user_id": "2", "username": "frontdesk", "role": "staff"}})
    monkeypatch.setattr(security, "_redis", lambda: fake)

    assert security.get_session("pre-fix") is None
    assert key in fake.deleted
    assert fake.expire_calls == []


def test_get_session_rejects_a_session_past_the_absolute_ttl_even_if_recently_active(monkeypatch):
    # Idle refresh alone would keep this alive forever; the absolute cap must
    # still cut it off once total lifetime exceeds
    # settings.absolute_session_timeout_seconds, independent of activity.
    key = "session:stale"
    created_at = 1000.0
    fake = _FakeRedis(store={
        key: {"user_id": "2", "username": "frontdesk", "role": "staff", "created_at": str(created_at)}
    })
    monkeypatch.setattr(security, "_redis", lambda: fake)
    monkeypatch.setattr(
        security, "_now", lambda: created_at + security.settings.absolute_session_timeout_seconds + 1
    )

    assert security.get_session("stale") is None
    assert key in fake.deleted
    assert fake.expire_calls == []  # never refreshed once past the absolute cap


def test_get_session_accepts_a_session_within_the_absolute_ttl(monkeypatch):
    key = "session:fresh"
    created_at = 1000.0
    fake = _FakeRedis(store={
        key: {"user_id": "2", "username": "frontdesk", "role": "staff", "created_at": str(created_at)}
    })
    monkeypatch.setattr(security, "_redis", lambda: fake)
    monkeypatch.setattr(
        security, "_now", lambda: created_at + security.settings.absolute_session_timeout_seconds - 1
    )

    data = security.get_session("fresh")

    assert data["user_id"] == "2"
    assert fake.expire_calls == [(key, security.settings.session_timeout_seconds)]
