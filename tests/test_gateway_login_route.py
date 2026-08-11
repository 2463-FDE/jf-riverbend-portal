"""Route-level tests for POST /login and POST /login/mfa — production-
readiness Stage 1 item 3 (functional TOTP MFA).

Runs the real login/login_mfa code paths (including the User ORM model)
against an in-memory SQLite engine instead of Postgres, and fakes the MFA
challenge store / session minting the same way other gateway route tests
fake get_session (monkeypatching the names app.py's route functions look up
in its own module namespace) — Redis mechanics themselves are covered
separately in test_gateway_security.py.
"""
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_login")


class _FakeChallengeStore:
    def __init__(self):
        self._store = {}
        self._next = 0

    def create(self, user_id):
        self._next += 1
        token = f"tok{self._next}"
        self._store[token] = user_id
        return token

    def get(self, token):
        return self._store.get(token)

    def destroy(self, token):
        self._store.pop(token, None)


@pytest.fixture
def env(monkeypatch):
    # StaticPool: a bare ":memory:" engine otherwise hands every new
    # connection a SEPARATE, empty in-memory database — sessionmaker's
    # per-call connection would never see create_all's tables.
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    app_mod.User.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine)

    def fake_get_db():
        db = session_local()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_get_db

    challenges = _FakeChallengeStore()
    monkeypatch.setattr(app_mod, "create_mfa_challenge", challenges.create)
    monkeypatch.setattr(app_mod, "get_mfa_challenge", challenges.get)
    monkeypatch.setattr(app_mod, "destroy_mfa_challenge", challenges.destroy)
    monkeypatch.setattr(app_mod, "create_session", lambda uid, uname, role: f"session-for-{uid}")

    client = TestClient(app_mod.app)
    yield client, session_local
    app_mod.app.dependency_overrides.clear()


from security import hash_password  # already in sys.modules from loading app_mod above


def _seed_user(session_local, username="frontdesk", mfa_secret=None):
    db = session_local()
    user = app_mod.User(
        username=username,
        password_hash=hash_password("portal123"),
        full_name="Front Desk",
        role="staff",
        mfa_secret=mfa_secret,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user.id


def test_login_without_mfa_required_issues_session_directly(env, monkeypatch):
    client, session_local = env
    monkeypatch.setattr(app_mod.roles_config, "mfa_required", lambda: False)
    _seed_user(session_local)

    resp = client.post("/login", json={"username": "frontdesk", "password": "portal123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"].startswith("session-for-")
    assert body["mfa"] is False


def test_login_rejects_wrong_password_regardless_of_mfa(env, monkeypatch):
    client, session_local = env
    monkeypatch.setattr(app_mod.roles_config, "mfa_required", lambda: True)
    _seed_user(session_local)

    resp = client.post("/login", json={"username": "frontdesk", "password": "wrong"})

    assert resp.status_code == 401


def test_login_with_mfa_required_and_no_enrollment_returns_enrollment_challenge(env, monkeypatch):
    client, session_local = env
    monkeypatch.setattr(app_mod.roles_config, "mfa_required", lambda: True)
    _seed_user(session_local, mfa_secret=None)

    resp = client.post("/login", json={"username": "frontdesk", "password": "portal123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mfa_enrollment_required"] is True
    assert body["pending_mfa_token"]
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert "token" not in body  # no session yet — enrollment isn't confirmed


def test_login_with_mfa_required_and_existing_secret_returns_challenge_only(env, monkeypatch):
    client, session_local = env
    monkeypatch.setattr(app_mod.roles_config, "mfa_required", lambda: True)
    secret = pyotp.random_base32()
    _seed_user(session_local, mfa_secret=secret)

    resp = client.post("/login", json={"username": "frontdesk", "password": "portal123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mfa_required"] is True
    assert body["pending_mfa_token"]
    assert "otpauth_uri" not in body  # already enrolled — no secret re-issued
    assert "token" not in body


def test_login_mfa_accepts_correct_code_and_issues_session(env, monkeypatch):
    client, session_local = env
    monkeypatch.setattr(app_mod.roles_config, "mfa_required", lambda: True)
    secret = pyotp.random_base32()
    user_id = _seed_user(session_local, mfa_secret=secret)

    login_resp = client.post("/login", json={"username": "frontdesk", "password": "portal123"})
    pending_token = login_resp.json()["pending_mfa_token"]

    code = pyotp.TOTP(secret).now()
    resp = client.post("/login/mfa", json={"pending_mfa_token": pending_token, "code": code})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == f"session-for-{user_id}"
    assert body["mfa"] is True


def test_login_mfa_rejects_wrong_code_without_consuming_the_challenge(env, monkeypatch):
    client, session_local = env
    monkeypatch.setattr(app_mod.roles_config, "mfa_required", lambda: True)
    secret = pyotp.random_base32()
    _seed_user(session_local, mfa_secret=secret)

    login_resp = client.post("/login", json={"username": "frontdesk", "password": "portal123"})
    pending_token = login_resp.json()["pending_mfa_token"]

    wrong_code = str((int(pyotp.TOTP(secret).now()) + 1) % 1_000_000).zfill(6)
    bad = client.post("/login/mfa", json={"pending_mfa_token": pending_token, "code": wrong_code})
    assert bad.status_code == 401

    # The challenge must still be usable — a mistyped code shouldn't lock
    # the caller out for the rest of its TTL window.
    good_code = pyotp.TOTP(secret).now()
    ok = client.post("/login/mfa", json={"pending_mfa_token": pending_token, "code": good_code})
    assert ok.status_code == 200


def test_login_mfa_rejects_an_expired_or_unknown_token(env):
    client, _ = env

    resp = client.post("/login/mfa", json={"pending_mfa_token": "does-not-exist", "code": "123456"})

    assert resp.status_code == 401


def test_login_mfa_first_success_confirms_enrollment_and_completes_first_login_end_to_end(env, monkeypatch):
    # Full round trip: unenrolled -> /login mints a secret -> /login/mfa with
    # the correct code both confirms enrollment and returns a session, in
    # one continuous flow a real client would drive.
    client, session_local = env
    monkeypatch.setattr(app_mod.roles_config, "mfa_required", lambda: True)
    _seed_user(session_local, mfa_secret=None)

    enroll = client.post("/login", json={"username": "frontdesk", "password": "portal123"})
    otpauth_uri = enroll.json()["otpauth_uri"]
    secret = dict(part.split("=") for part in otpauth_uri.split("?", 1)[1].split("&"))["secret"]
    pending_token = enroll.json()["pending_mfa_token"]

    code = pyotp.TOTP(secret).now()
    resp = client.post("/login/mfa", json={"pending_mfa_token": pending_token, "code": code})

    assert resp.status_code == 200
    assert resp.json()["token"]
    assert resp.json()["mfa"] is True
