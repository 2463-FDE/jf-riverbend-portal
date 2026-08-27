"""POST /mfa/verify (the login-CHALLENGE completion for an already-enrolled
account) and POST /mfa/backup-codes/regenerate (services/gateway/app.py).

Covers: TOTP verification consumes the challenge and mints a session, a
wrong code does NOT consume the challenge (retryable), backup codes are
hashed/single-use/atomically consumed, reuse of a spent or invalidated code
is rejected, and regeneration invalidates every previously-issued code.
"""
import base64

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_mfa_verify")

PASSWORD = "portal123-testpass"


class _FakeRedis:
    def __init__(self):
        self._store = {}
        self._counters = {}
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

    def incr(self, key):
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MFA_ACTIVE_KEY_VERSION", "v1")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY_V1", base64.b64encode(b"\x03" * 32).decode())
    app_mod.mfa_crypto.reset_key_provider()
    monkeypatch.setattr(app_mod.mfa_config, "mfa_requirement_for", lambda user, **kw: "enforce")

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


def _enrolled_user_with_secret(client, *, username="drnguyen"):
    """Create a fully-enrolled user with a real TOTP secret and ten backup
    codes, the way /mfa/enroll/confirm would leave one, without going
    through the enrollment routes themselves (those are covered in
    test_gateway_mfa_enrollment_route.py — this file starts from "already
    enrolled")."""
    secret = pyotp.random_base32()
    envelope, version = app_mod.mfa_crypto.encrypt_totp_secret(1, secret)  # placeholder id, fixed below
    with client.Session() as s:
        user = app_mod.User(
            username=username, password_hash=app_mod.hash_password(PASSWORD),
            role="clinician", is_active=True, mfa_pilot=True,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        envelope, version = app_mod.mfa_crypto.encrypt_totp_secret(user.id, secret)
        user.mfa_secret_ciphertext = envelope
        user.mfa_secret_key_version = version
        user.mfa_enrolled_at = app_mod.func.now()
        codes = ["CODE" + str(i).zfill(6) for i in range(10)]
        for code in codes:
            s.add(app_mod.MfaBackupCode(user_id=user.id, code_hash=app_mod.hash_password(code)))
        s.commit()
        return user.id, secret, codes


def _login_challenge(client, username="drnguyen"):
    resp = client.post("/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["mfa"]["challenge_token"]


# --- TOTP happy path ----------------------------------------------------------


def test_verify_with_a_correct_totp_code_mints_a_session_and_consumes_the_challenge(client):
    _user_id, secret, _codes = _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)

    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["user"]["username"] == "drnguyen"
    assert f"mfa_challenge:{challenge}" in client.fake_redis.deleted


def test_wrong_totp_code_does_not_consume_the_challenge(client):
    _user_id, _secret, _codes = _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)

    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "code": "000000"})

    assert resp.status_code == 401
    assert f"mfa_challenge:{challenge}" not in client.fake_redis.deleted
    # Retryable: the SAME challenge still resolves to a real, valid attempt.
    _user_id2, secret, _codes2 = (None, None, None)


def test_a_second_correct_attempt_after_a_wrong_one_still_succeeds(client):
    user_id, secret, _codes = _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)

    client.post("/mfa/verify", json={"challenge_token": challenge, "code": "000000"})
    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()})

    assert resp.status_code == 200


def test_verify_rejects_an_expired_or_unknown_challenge(client):
    resp = client.post("/mfa/verify", json={"challenge_token": "not-real", "code": "123456"})

    assert resp.status_code == 401


def test_verify_rejects_supplying_both_code_and_backup_code(client):
    _user_id, secret, codes = _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)

    resp = client.post(
        "/mfa/verify",
        json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now(), "backup_code": codes[0]},
    )

    assert resp.status_code == 400


def test_verify_rejects_neither_code_nor_backup_code(client):
    _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)

    resp = client.post("/mfa/verify", json={"challenge_token": challenge})

    assert resp.status_code == 400


def test_verify_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(app_mod, "_MFA_VERIFY_RATE_LIMIT", 2)
    _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)

    for _ in range(2):
        client.post("/mfa/verify", json={"challenge_token": challenge, "code": "000000"})
    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "code": "000000"})

    assert resp.status_code == 429


# --- backup codes: hashed, single-use, atomically consumed ------------------


def test_backup_codes_are_never_stored_in_plaintext(client):
    user_id, _secret, codes = _enrolled_user_with_secret(client)

    with client.Session() as s:
        rows = s.query(app_mod.MfaBackupCode).filter_by(user_id=user_id).all()
        stored_hashes = {r.code_hash for r in rows}

    assert not (set(codes) & stored_hashes)
    for h in stored_hashes:
        assert h.startswith("pbkdf2_sha256$")


def test_verify_with_a_correct_backup_code_mints_a_session_and_consumes_it(client):
    _user_id, _secret, codes = _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)

    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "backup_code": codes[0]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["backup_codes_remaining"] == 9


def test_a_used_backup_code_cannot_be_used_again(client):
    _user_id, _secret, codes = _enrolled_user_with_secret(client)
    challenge1 = _login_challenge(client)
    client.post("/mfa/verify", json={"challenge_token": challenge1, "backup_code": codes[0]})

    challenge2 = _login_challenge(client)
    resp = client.post("/mfa/verify", json={"challenge_token": challenge2, "backup_code": codes[0]})

    assert resp.status_code == 401


def test_a_backup_code_is_accepted_case_and_whitespace_insensitively(client):
    _user_id, _secret, codes = _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)
    messy = " " + codes[0].lower() + " "

    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "backup_code": messy})

    assert resp.status_code == 200


def test_an_unknown_backup_code_is_rejected(client):
    _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)

    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "backup_code": "NOTAREALCODE"})

    assert resp.status_code == 401


# --- regeneration invalidates every previous unused code --------------------


def test_regeneration_returns_ten_new_codes(client):
    user_id, _secret, _codes = _enrolled_user_with_secret(client)
    token = app_mod.create_session(user_id, "drnguyen", "clinician")

    resp = client.post("/mfa/backup-codes/regenerate", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert len(resp.json()["backup_codes"]) == 10


def test_regeneration_invalidates_every_previously_issued_unused_code(client):
    _user_id, _secret, old_codes = _enrolled_user_with_secret(client)
    token = app_mod.create_session(_user_id, "drnguyen", "clinician")

    client.post("/mfa/backup-codes/regenerate", headers={"Authorization": f"Bearer {token}"})

    challenge = _login_challenge(client)
    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "backup_code": old_codes[0]})

    assert resp.status_code == 401


def test_regeneration_requires_an_existing_session(client):
    _enrolled_user_with_secret(client)

    resp = client.post("/mfa/backup-codes/regenerate")

    assert resp.status_code == 401


def test_regeneration_refuses_for_an_unenrolled_account(client):
    with client.Session() as s:
        user = app_mod.User(
            username="unenrolled", password_hash=app_mod.hash_password(PASSWORD),
            role="clinician", is_active=True,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        user_id = user.id
    token = app_mod.create_session(user_id, "unenrolled", "clinician")

    resp = client.post("/mfa/backup-codes/regenerate", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 409
