"""POST /login across MFA rollout modes (services/gateway/app.py).

Full-stack route tests: real FastAPI TestClient, SQLite in-memory `users`
table, a fake Redis wired through security.py's real module globals (the
same __globals__ trick tests/conftest.py's phi_globals_of documents — app.py
does `from security import create_mfa_challenge, ...`, so those names are
bound directly on app_mod, and app_mod.create_mfa_challenge.__globals__ IS
security.py's own module namespace, regardless of collection-order effects
on sys.modules['security'] from other test files).

mfa_config and mfa_crypto are imported as WHOLE modules in app.py (`import
mfa_config`), so app_mod.mfa_config / app_mod.mfa_crypto are the real module
objects directly — no globals indirection needed for those two.
"""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_login")

PASSWORD = "portal123-testpass"


class _FakeRedis:
    def __init__(self):
        self._store = {}
        self._counters = {}
        self._ttls = {}

    def hset(self, key, mapping=None):
        self._store[key] = dict(mapping)

    def hgetall(self, key):
        return dict(self._store.get(key, {}))

    def expire(self, key, ttl):
        self._ttls[key] = ttl

    def delete(self, key):
        self._store.pop(key, None)
        self._counters.pop(key, None)

    def incr(self, key):
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]


@pytest.fixture
def db_session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    app_mod.User.metadata.create_all(engine)
    app_mod.MfaBackupCode.metadata.create_all(engine)
    app_mod.AuditLog.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client(monkeypatch, db_session_factory):
    monkeypatch.setenv("MFA_ACTIVE_KEY_VERSION", "v1")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY_V1", base64.b64encode(b"\x01" * 32).decode())
    app_mod.mfa_crypto.reset_key_provider()

    fake = _FakeRedis()
    security_globals = app_mod.create_mfa_challenge.__globals__
    monkeypatch.setitem(security_globals, "_redis", lambda: fake)

    def fake_db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_db
    tc = TestClient(app_mod.app)
    tc.fake_redis = fake
    tc.Session = db_session_factory
    yield tc
    app_mod.app.dependency_overrides.clear()


def _mfa_config(tmp_path, monkeypatch, text: str):
    path = tmp_path / "mfa.yaml"
    path.write_text(text)
    monkeypatch.setattr(app_mod.mfa_config, "_MFA_CONFIG_PATH", str(path))
    app_mod.mfa_config.reload()


def _make_user(client, *, username="drnguyen", role="clinician", pilot=False, shared=False, enrolled=False):
    with client.Session() as s:
        user = app_mod.User(
            username=username,
            password_hash=app_mod.hash_password(PASSWORD),
            role=role,
            is_active=True,
            mfa_pilot=pilot,
            mfa_shared_account=shared,
        )
        if enrolled:
            user.mfa_enrolled_at = app_mod.func.now()
        s.add(user)
        s.commit()
        s.refresh(user)
        return user.id


def _login(client, username="drnguyen", password=PASSWORD):
    return client.post("/login", json={"username": username, "password": password})


# --- mode: off — password-only, unaffected, for every account -------------


def test_mode_off_mints_a_session_directly(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: "off"\n')
    _make_user(client, pilot=True)  # even a pilot account is unaffected while off

    resp = _login(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["mfa"]["required"] is False


def test_mode_off_rejects_wrong_password(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: "off"\n')
    _make_user(client)

    resp = _login(client, password="wrong")

    assert resp.status_code == 401


# --- mode: prompt — never blocks login, nudges only ------------------------


def test_prompt_mode_still_mints_a_session_for_an_unenrolled_pilot_account(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: prompt\nscope: pilot\n')
    _make_user(client, pilot=True)

    resp = _login(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["mfa"]["required"] is False
    assert body["mfa"]["prompt"] is True
    assert body["mfa"]["enrolled"] is False


def test_prompt_mode_does_not_prompt_an_out_of_scope_account(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: prompt\nscope: pilot\n')
    _make_user(client, pilot=False)

    resp = _login(client)

    assert resp.status_code == 200
    assert resp.json()["mfa"]["prompt"] is False


def test_prompt_mode_does_not_prompt_an_already_enrolled_account(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: prompt\nscope: pilot\n')
    _make_user(client, pilot=True, enrolled=True)

    resp = _login(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["mfa"]["prompt"] is False
    assert body["mfa"]["enrolled"] is True


# --- mode: enforce — blocks session issuance for in-scope accounts ---------


def test_enforce_mode_does_not_mint_a_session_for_an_unenrolled_pilot_account(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: enforce\nscope: pilot\n')
    _make_user(client, pilot=True)

    resp = _login(client)

    assert resp.status_code == 200  # password WAS correct — not an error
    body = resp.json()
    assert "token" not in body
    assert body["mfa"]["required"] is True
    assert body["mfa"]["enrollment_required"] is True
    assert body["mfa"]["challenge_token"]


def test_enforce_mode_issues_a_login_challenge_for_an_enrolled_account(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: enforce\nscope: pilot\n')
    _make_user(client, pilot=True, enrolled=True)

    resp = _login(client)

    body = resp.json()
    assert "token" not in body
    assert body["mfa"]["required"] is True
    assert body["mfa"]["enrollment_required"] is False


def test_enforce_mode_still_rejects_a_wrong_password_before_any_mfa_branch(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: enforce\nscope: pilot\n')
    _make_user(client, pilot=True)

    resp = _login(client, password="wrong")

    assert resp.status_code == 401


def test_enforce_mode_does_not_affect_an_out_of_scope_account(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: enforce\nscope: pilot\n')
    _make_user(client, pilot=False)

    resp = _login(client)

    assert resp.status_code == 200
    assert resp.json()["token"]


def test_enforce_mode_never_reaches_a_shared_account_even_under_scope_all(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: enforce\nscope: all\n')
    _make_user(client, shared=True, pilot=True)

    resp = _login(client)

    assert resp.status_code == 200
    assert resp.json()["token"]  # password-only, unaffected — shared accounts are never enforced


# --- rate limiting -----------------------------------------------------------


def test_login_is_rate_limited_per_username(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: "off"\n')
    monkeypatch.setattr(app_mod, "_LOGIN_RATE_LIMIT", 3)
    _make_user(client)

    for _ in range(3):
        _login(client, password="wrong")
    resp = _login(client, password="wrong")

    assert resp.status_code == 429


def test_login_rate_limit_does_not_block_a_different_username(client, tmp_path, monkeypatch):
    _mfa_config(tmp_path, monkeypatch, 'mode: "off"\n')
    monkeypatch.setattr(app_mod, "_LOGIN_RATE_LIMIT", 1)
    _make_user(client, username="drnguyen")
    _make_user(client, username="drkim")

    _login(client, username="drnguyen", password="wrong")
    resp = _login(client, username="drkim", password=PASSWORD)

    assert resp.status_code == 200
