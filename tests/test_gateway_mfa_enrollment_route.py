"""POST /mfa/enroll/start and /mfa/enroll/confirm (services/gateway/app.py).

Covers: enrollment stays pending until a correct code confirms it, wrong/
replayed codes are rejected, confirmation generates backup codes and (for
the forced-enrollment-via-challenge path) mints a real session in the same
call, and the voluntary in-session path does not mint a second session.
"""
import base64

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_mfa_enroll")

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

    def incr(self, key):
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MFA_ACTIVE_KEY_VERSION", "v1")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY_V1", base64.b64encode(b"\x02" * 32).decode())
    app_mod.mfa_crypto.reset_key_provider()

    monkeypatch.setattr(app_mod.mfa_config, "mfa_requirement_for", lambda user, **kw: "enforce")
    monkeypatch.setattr(app_mod.mfa_config, "effective_mode", lambda **kw: "enforce")

    fake = _FakeRedis()
    security_globals = app_mod.create_mfa_challenge.__globals__
    monkeypatch.setitem(security_globals, "_redis", lambda: fake)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    app_mod.User.metadata.create_all(engine)
    app_mod.MfaBackupCode.metadata.create_all(engine)
    app_mod.AuditLog.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def fake_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_db
    tc = TestClient(app_mod.app)
    tc.Session = Session
    tc.fake_redis = fake
    yield tc
    app_mod.app.dependency_overrides.clear()


def _make_user(client, **overrides):
    defaults = dict(username="drnguyen", role="clinician", is_active=True, mfa_pilot=True)
    defaults.update(overrides)
    with client.Session() as s:
        user = app_mod.User(password_hash=app_mod.hash_password(PASSWORD), **defaults)
        s.add(user)
        s.commit()
        s.refresh(user)
        return user.id


def _login_challenge(client, username="drnguyen"):
    resp = client.post("/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["mfa"]["challenge_token"]


def _real_session_token(client, user_id, username="drnguyen", role="clinician"):
    return app_mod.create_session(user_id, username, role)


# --- enrollment stays pending until confirmed -------------------------------


def test_enroll_start_returns_a_uri_and_manual_key_but_does_not_activate_enrollment(client):
    user_id = _make_user(client)
    challenge = _login_challenge(client)

    resp = client.post("/mfa/enroll/start", json={"challenge_token": challenge})

    assert resp.status_code == 200
    body = resp.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert body["manual_entry_key"]

    with client.Session() as s:
        user = s.get(app_mod.User, user_id)
        assert user.mfa_secret_ciphertext is not None
        assert user.mfa_enrolled_at is None  # PENDING, not active


def test_enroll_start_twice_replaces_the_pending_secret(client):
    _make_user(client)
    challenge = _login_challenge(client)

    first = client.post("/mfa/enroll/start", json={"challenge_token": challenge}).json()
    second = client.post("/mfa/enroll/start", json={"challenge_token": challenge}).json()

    assert first["manual_entry_key"] != second["manual_entry_key"]
    # The old secret no longer verifies — only the second one does.
    old_code = pyotp.TOTP(first["manual_entry_key"]).now()
    resp = client.post("/mfa/enroll/confirm", json={"challenge_token": challenge, "code": old_code})
    assert resp.status_code == 401


# --- correct confirmation activates MFA -------------------------------------


def test_correct_confirmation_activates_enrollment_and_returns_ten_backup_codes(client):
    user_id = _make_user(client)
    challenge = _login_challenge(client)
    secret = client.post("/mfa/enroll/start", json={"challenge_token": challenge}).json()["manual_entry_key"]
    code = pyotp.TOTP(secret).now()

    resp = client.post("/mfa/enroll/confirm", json={"challenge_token": challenge, "code": code})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "enrolled"
    assert len(body["backup_codes"]) == 10
    assert len(set(body["backup_codes"])) == 10

    with client.Session() as s:
        user = s.get(app_mod.User, user_id)
        assert user.mfa_enrolled_at is not None


def test_confirmation_via_a_login_challenge_mints_a_real_session(client):
    _make_user(client)
    challenge = _login_challenge(client)
    secret = client.post("/mfa/enroll/start", json={"challenge_token": challenge}).json()["manual_entry_key"]
    code = pyotp.TOTP(secret).now()

    resp = client.post("/mfa/enroll/confirm", json={"challenge_token": challenge, "code": code})

    body = resp.json()
    assert body["token"]
    assert body["user"]["username"] == "drnguyen"

    # A second /login for the same account, with the SAME (now stale)
    # challenge, must fail — the challenge was consumed by confirmation.
    resp2 = client.post("/mfa/enroll/start", json={"challenge_token": challenge})
    assert resp2.status_code == 401


def test_voluntary_confirmation_via_an_existing_session_does_not_mint_a_second_session(client):
    user_id = _make_user(client)
    token = _real_session_token(client, user_id)
    start = client.post(
        "/mfa/enroll/start", json={}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    code = pyotp.TOTP(start["manual_entry_key"]).now()

    resp = client.post(
        "/mfa/enroll/confirm", json={"code": code}, headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "enrolled"
    assert "token" not in body  # the caller already had a session; nothing new minted


# --- wrong / expired / replayed codes ---------------------------------------


def test_confirm_rejects_a_wrong_code(client):
    _make_user(client)
    challenge = _login_challenge(client)
    client.post("/mfa/enroll/start", json={"challenge_token": challenge})

    resp = client.post("/mfa/enroll/confirm", json={"challenge_token": challenge, "code": "000000"})

    assert resp.status_code == 401
    with client.Session() as s:
        user = s.execute(app_mod.select(app_mod.User).where(app_mod.User.username == "drnguyen")).scalar_one()
        assert user.mfa_enrolled_at is None


def test_confirm_rejects_an_expired_or_unknown_challenge(client):
    resp = client.post("/mfa/enroll/confirm", json={"challenge_token": "not-a-real-token", "code": "123456"})

    assert resp.status_code == 401


def test_confirm_rejects_a_replayed_code_after_it_already_succeeded(client):
    user_id = _make_user(client)
    token = _real_session_token(client, user_id)
    start = client.post(
        "/mfa/enroll/start", json={}, headers={"Authorization": f"Bearer {token}"}
    ).json()
    secret = start["manual_entry_key"]
    code = pyotp.TOTP(secret).now()

    first = client.post(
        "/mfa/enroll/confirm", json={"code": code}, headers={"Authorization": f"Bearer {token}"}
    )
    assert first.status_code == 200

    # Already enrolled — confirming again (even with a freshly valid code
    # for a NEW hypothetical secret) must not be reachable a second time.
    second = client.post(
        "/mfa/enroll/confirm", json={"code": code}, headers={"Authorization": f"Bearer {token}"}
    )
    assert second.status_code == 409


def test_confirm_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(app_mod, "_MFA_CONFIRM_RATE_LIMIT", 2)
    _make_user(client)
    challenge = _login_challenge(client)
    client.post("/mfa/enroll/start", json={"challenge_token": challenge})

    for _ in range(2):
        client.post("/mfa/enroll/confirm", json={"challenge_token": challenge, "code": "000000"})
    resp = client.post("/mfa/enroll/confirm", json={"challenge_token": challenge, "code": "000000"})

    assert resp.status_code == 429


# --- accounts this rollout has not reached ----------------------------------


def test_enroll_start_refuses_for_an_out_of_scope_account(client, monkeypatch):
    monkeypatch.setattr(app_mod.mfa_config, "mfa_requirement_for", lambda user, **kw: "off")
    user_id = _make_user(client, mfa_pilot=False)
    token = _real_session_token(client, user_id)

    resp = client.post("/mfa/enroll/start", json={}, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403


def test_enroll_start_refuses_for_an_already_enrolled_account(client):
    user_id = _make_user(client, mfa_enrolled_at=app_mod.func.now())
    token = _real_session_token(client, user_id)

    resp = client.post("/mfa/enroll/start", json={}, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 409


def test_enroll_start_refuses_without_a_session_or_challenge(client):
    resp = client.post("/mfa/enroll/start", json={})

    assert resp.status_code == 401


# --- B01 (Round-1 review): exactly one authentication source, never both ---
#
# A request naming BOTH a session and a challenge_token could be a browser
# genuinely holding a stale session for one account alongside a fresh
# challenge for a different login — _resolve_mfa_principal used to try the
# session first and silently ignore challenge_token, which would run this
# route against whichever user the leftover session named instead of the
# user the challenge is actually for.


def test_enroll_start_rejects_a_session_and_challenge_naming_different_users(client):
    user_a = _make_user(client, username="user-a")
    user_b = _make_user(client, username="user-b")
    token_a = _real_session_token(client, user_a, username="user-a")
    challenge_b = _login_challenge(client, username="user-b")

    resp = client.post(
        "/mfa/enroll/start",
        json={"challenge_token": challenge_b},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    assert resp.status_code == 400
    with client.Session() as s:
        assert s.get(app_mod.User, user_a).mfa_secret_ciphertext is None
        assert s.get(app_mod.User, user_b).mfa_secret_ciphertext is None


def test_enroll_start_rejects_a_session_and_challenge_for_the_same_user(client):
    # "Exactly one source" is the contract, not "at most one, unless they
    # happen to agree" — a matching principal does not earn an exception.
    user_id = _make_user(client)
    token = _real_session_token(client, user_id)
    challenge = _login_challenge(client)

    resp = client.post(
        "/mfa/enroll/start",
        json={"challenge_token": challenge},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    with client.Session() as s:
        assert s.get(app_mod.User, user_id).mfa_secret_ciphertext is None


def test_enroll_confirm_also_rejects_a_session_and_challenge_naming_different_users(client):
    user_a = _make_user(client, username="user-a")
    user_b = _make_user(client, username="user-b")
    token_a = _real_session_token(client, user_a, username="user-a")
    challenge_b = _login_challenge(client, username="user-b")

    resp = client.post(
        "/mfa/enroll/confirm",
        json={"challenge_token": challenge_b, "code": "000000"},
        headers={"Authorization": f"Bearer {token_a}"},
    )

    assert resp.status_code == 400


# --- Round-2 review: enrollment state/audit transaction and atomic claim -----


def _fail_audit_inserts(session, _flush_context, _instances):
    if any(isinstance(obj, app_mod.AuditLog) for obj in session.new):
        raise SQLAlchemyError("forced audit insert failure")


def test_enroll_start_rolls_back_the_secret_when_its_audit_insert_fails(client):
    user_id = _make_user(client)
    challenge = _login_challenge(client)
    event.listen(client.Session.class_, "before_flush", _fail_audit_inserts)
    try:
        resp = client.post("/mfa/enroll/start", json={"challenge_token": challenge})
    finally:
        event.remove(client.Session.class_, "before_flush", _fail_audit_inserts)

    assert resp.status_code == 503
    with client.Session() as s:
        user = s.get(app_mod.User, user_id)
        assert user.mfa_secret_ciphertext is None
        assert user.mfa_secret_key_version is None


def test_enroll_confirm_rolls_back_state_and_keeps_challenge_when_audit_insert_fails(client, monkeypatch):
    user_id = _make_user(client)
    challenge = _login_challenge(client)
    secret = client.post("/mfa/enroll/start", json={"challenge_token": challenge}).json()["manual_entry_key"]
    code = pyotp.TOTP(secret).now()
    create_session_calls = []
    monkeypatch.setattr(
        app_mod,
        "create_session",
        lambda *args, **kwargs: create_session_calls.append(args) or "unexpected-session",
    )

    event.listen(client.Session.class_, "before_flush", _fail_audit_inserts)
    try:
        resp = client.post("/mfa/enroll/confirm", json={"challenge_token": challenge, "code": code})
    finally:
        event.remove(client.Session.class_, "before_flush", _fail_audit_inserts)

    assert resp.status_code == 503
    assert create_session_calls == []
    assert f"mfa_challenge:{challenge}" in client.fake_redis._store
    with client.Session() as s:
        user = s.get(app_mod.User, user_id)
        assert user.mfa_enrolled_at is None
        assert user.mfa_last_totp_step is None
        assert s.query(app_mod.MfaBackupCode).filter_by(user_id=user_id).count() == 0


def test_only_one_atomic_claim_can_confirm_a_pending_enrollment(client):
    user_id = _make_user(client)
    challenge = _login_challenge(client)
    client.post("/mfa/enroll/start", json={"challenge_token": challenge})

    with client.Session() as s:
        user = s.get(app_mod.User, user_id)
        kwargs = {
            "user_id": user_id,
            "candidate_step": 500,
            "expected_ciphertext": user.mfa_secret_ciphertext,
            "expected_key_version": user.mfa_secret_key_version,
            "mark_login": True,
        }
        first = app_mod._claim_mfa_enrollment(s, **kwargs)
        second = app_mod._claim_mfa_enrollment(s, **kwargs)
        s.commit()

    assert (first, second) == (True, False)


def test_losing_enrollment_confirmation_generates_no_codes_or_session(client, monkeypatch):
    user_id = _make_user(client)
    challenge = _login_challenge(client)
    secret = client.post("/mfa/enroll/start", json={"challenge_token": challenge}).json()["manual_entry_key"]
    code = pyotp.TOTP(secret).now()
    monkeypatch.setattr(app_mod, "_claim_mfa_enrollment", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        app_mod,
        "_generate_and_store_backup_codes",
        lambda *args, **kwargs: pytest.fail("losing confirmation generated backup codes"),
    )
    monkeypatch.setattr(
        app_mod,
        "create_session",
        lambda *args, **kwargs: pytest.fail("losing confirmation minted a session"),
    )

    resp = client.post("/mfa/enroll/confirm", json={"challenge_token": challenge, "code": code})

    assert resp.status_code == 409
    assert f"mfa_challenge:{challenge}" in client.fake_redis._store
    with client.Session() as s:
        assert s.query(app_mod.MfaBackupCode).filter_by(user_id=user_id).count() == 0
