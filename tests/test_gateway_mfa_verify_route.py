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
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
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


# --- M01 (Round-1 review): the backup-code claim is atomic against an
# invalidation racing in between the SELECT scan and the claiming UPDATE ---


def test_claim_backup_code_helper_rejects_a_row_invalidated_after_selection(client):
    # The exact scenario the finding describes, as a direct unit test of the
    # extracted helper: invalidate the row, THEN call the real guarded
    # UPDATE, and confirm it returns False rather than claiming anyway.
    user_id, _secret, codes = _enrolled_user_with_secret(client)
    with client.Session() as s:
        target = s.query(app_mod.MfaBackupCode).filter_by(user_id=user_id).first()
        target_id = target.id
        target.invalidated_at = app_mod.func.now()
        s.commit()

        claimed = app_mod._claim_backup_code(s, backup_code_id=target_id, user_id=user_id)

        assert claimed is False
        s.rollback()
        row = s.get(app_mod.MfaBackupCode, target_id)
        assert row.used_at is None  # never claimed despite the helper running


def test_claim_backup_code_helper_rejects_a_row_owned_by_a_different_user(client):
    user_a, _secret_a, codes_a = _enrolled_user_with_secret(client, username="user-a")
    user_b, _secret_b, _codes_b = _enrolled_user_with_secret(client, username="user-b")
    with client.Session() as s:
        row_a = s.query(app_mod.MfaBackupCode).filter_by(user_id=user_a).first()

        claimed = app_mod._claim_backup_code(s, backup_code_id=row_a.id, user_id=user_b)

        assert claimed is False
        s.rollback()
        row = s.get(app_mod.MfaBackupCode, row_a.id)
        assert row.used_at is None


def test_verify_rejects_a_code_invalidated_between_the_scan_and_the_claim(client, monkeypatch):
    # Simulates the real race: a regeneration or reset commits its
    # invalidation in the window between this route's SELECT-time hash scan
    # and the claiming UPDATE. verify_password is the call that sits right
    # at that boundary (it runs once per candidate row, inside the scan
    # loop) — wrapping it to invalidate the matched row the instant it
    # would otherwise succeed reproduces the race deterministically, without
    # threads.
    user_id, _secret, codes = _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)
    target_code = codes[0]

    real_verify_password = app_mod.verify_password
    invalidated = []

    def racy_verify_password(candidate, encoded):
        result = real_verify_password(candidate, encoded)
        if result and not invalidated:
            invalidated.append(True)
            # Only the ONE row this call is checking — a real concurrent
            # regeneration/reset would invalidate every active code, but
            # this test isolates the exact race (this row's own
            # invalidated_at flips between the SELECT scan and the
            # claiming UPDATE) rather than incidentally also removing the
            # "retry with a different, still-valid code" cross-check below.
            with client.Session() as s:
                s.execute(
                    app_mod.update(app_mod.MfaBackupCode)
                    .where(app_mod.MfaBackupCode.code_hash == encoded)
                    .values(invalidated_at=app_mod.func.now())
                )
                s.commit()
        return result

    monkeypatch.setattr(app_mod, "verify_password", racy_verify_password)
    create_session_calls = []
    monkeypatch.setattr(
        app_mod, "create_session",
        lambda *a, **k: create_session_calls.append(a) or "unexpected-session",
    )

    resp = client.post("/mfa/verify", json={"challenge_token": challenge, "backup_code": target_code})

    assert resp.status_code == 401
    assert create_session_calls == []
    # The challenge was never consumed — a genuinely valid factor (a
    # different, still-active backup code) still completes the login.
    assert f"mfa_challenge:{challenge}" not in client.fake_redis.deleted
    resp2 = client.post("/mfa/verify", json={"challenge_token": challenge, "backup_code": codes[1]})
    assert resp2.status_code == 200


# --- M02 (Round-1 review): monotonic TOTP steps + atomic step claim --------


def test_a_previous_step_code_is_rejected_after_the_current_step_was_accepted(client):
    user_id, secret, _codes = _enrolled_user_with_secret(client)
    totp = pyotp.TOTP(secret)
    current_step = int(__import__("time").time() // 30)

    challenge1 = _login_challenge(client)
    ok = client.post("/mfa/verify", json={"challenge_token": challenge1, "code": totp.generate_otp(current_step)})
    assert ok.status_code == 200

    challenge2 = _login_challenge(client)
    replay = client.post(
        "/mfa/verify", json={"challenge_token": challenge2, "code": totp.generate_otp(current_step - 1)}
    )
    assert replay.status_code == 401


def test_the_exact_same_step_is_rejected_on_a_second_attempt(client):
    user_id, secret, _codes = _enrolled_user_with_secret(client)
    code = pyotp.TOTP(secret).now()

    challenge1 = _login_challenge(client)
    first = client.post("/mfa/verify", json={"challenge_token": challenge1, "code": code})
    assert first.status_code == 200

    challenge2 = _login_challenge(client)
    second = client.post("/mfa/verify", json={"challenge_token": challenge2, "code": code})
    assert second.status_code == 401


def test_a_strictly_newer_step_succeeds_after_an_earlier_one_was_accepted(client):
    user_id, secret, _codes = _enrolled_user_with_secret(client)
    totp = pyotp.TOTP(secret)
    current_step = int(__import__("time").time() // 30)

    challenge1 = _login_challenge(client)
    first = client.post("/mfa/verify", json={"challenge_token": challenge1, "code": totp.generate_otp(current_step)})
    assert first.status_code == 200

    challenge2 = _login_challenge(client)
    second = client.post(
        "/mfa/verify", json={"challenge_token": challenge2, "code": totp.generate_otp(current_step + 1)}
    )
    assert second.status_code == 200


def test_a_replay_rejection_does_not_consume_the_challenge(client):
    user_id, secret, _codes = _enrolled_user_with_secret(client)
    code = pyotp.TOTP(secret).now()
    challenge1 = _login_challenge(client)
    client.post("/mfa/verify", json={"challenge_token": challenge1, "code": code})  # accepted

    challenge2 = _login_challenge(client)
    replay = client.post("/mfa/verify", json={"challenge_token": challenge2, "code": code})
    assert replay.status_code == 401
    assert f"mfa_challenge:{challenge2}" not in client.fake_redis.deleted

    # Retry the SAME challenge with a genuinely newer, valid code succeeds.
    totp = pyotp.TOTP(secret)
    current_step = int(__import__("time").time() // 30)
    retry = client.post(
        "/mfa/verify", json={"challenge_token": challenge2, "code": totp.generate_otp(current_step + 1)}
    )
    assert retry.status_code == 200


def _claim_totp_step(db, user_id, candidate_step):
    user = db.get(app_mod.User, user_id)
    return app_mod._claim_totp_step(
        db,
        user_id=user_id,
        candidate_step=candidate_step,
        expected_challenge_epoch=user.mfa_challenge_epoch,
        expected_ciphertext=user.mfa_secret_ciphertext,
        expected_key_version=user.mfa_secret_key_version,
    )


def test_claim_totp_step_helper_rejects_equal_and_lower_stored_values(client):
    user_id, _secret, _codes = _enrolled_user_with_secret(client)
    with client.Session() as s:
        user = s.get(app_mod.User, user_id)
        user.mfa_last_totp_step = 100
        s.commit()

        assert _claim_totp_step(s, user_id, 100) is False
        assert _claim_totp_step(s, user_id, 99) is False
        s.rollback()
        user = s.get(app_mod.User, user_id)
        assert user.mfa_last_totp_step == 100


def test_claim_totp_step_helper_accepts_a_strictly_greater_value(client):
    user_id, _secret, _codes = _enrolled_user_with_secret(client)
    with client.Session() as s:
        user = s.get(app_mod.User, user_id)
        user.mfa_last_totp_step = 100
        s.commit()

        assert _claim_totp_step(s, user_id, 101) is True
        s.commit()
        user = s.get(app_mod.User, user_id)
        assert user.mfa_last_totp_step == 101


def test_claim_totp_step_helper_accepts_the_first_ever_claim_from_null(client):
    user_id, _secret, _codes = _enrolled_user_with_secret(client)
    with client.Session() as s:
        assert _claim_totp_step(s, user_id, 1) is True


def test_two_concurrent_claims_of_the_same_step_only_one_succeeds(client):
    # Simulates two requests racing to persist the same accepted step —
    # exactly the case verify_code's own monotonic check cannot catch on
    # its own (both would compute the same valid step from two independent
    # calls). Only the guarded UPDATE decides the winner.
    user_id, _secret, _codes = _enrolled_user_with_secret(client)
    with client.Session() as s:
        first = _claim_totp_step(s, user_id, 500)
        second = _claim_totp_step(s, user_id, 500)
        s.commit()

    assert (first, second) == (True, False)


# --- Round-2 review: credential state and success audit commit together ------


def _fail_audit_inserts(session, _flush_context, _instances):
    if any(isinstance(obj, app_mod.AuditLog) for obj in session.new):
        raise SQLAlchemyError("forced audit insert failure")


def test_totp_claim_rolls_back_and_keeps_challenge_when_audit_insert_fails(client, monkeypatch):
    user_id, secret, _codes = _enrolled_user_with_secret(client)
    challenge = _login_challenge(client)
    create_session_calls = []
    monkeypatch.setattr(
        app_mod,
        "create_session",
        lambda *args, **kwargs: create_session_calls.append(args) or "unexpected-session",
    )

    event.listen(client.Session.class_, "before_flush", _fail_audit_inserts)
    try:
        resp = client.post(
            "/mfa/verify",
            json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()},
        )
    finally:
        event.remove(client.Session.class_, "before_flush", _fail_audit_inserts)

    assert resp.status_code == 503
    assert create_session_calls == []
    assert f"mfa_challenge:{challenge}" in client.fake_redis._store
    with client.Session() as s:
        assert s.get(app_mod.User, user_id).mfa_last_totp_step is None


def test_regeneration_rolls_back_old_code_invalidation_when_audit_insert_fails(client):
    user_id, _secret, _codes = _enrolled_user_with_secret(client)
    token = app_mod.create_session(user_id, "drnguyen", "clinician")

    event.listen(client.Session.class_, "before_flush", _fail_audit_inserts)
    try:
        resp = client.post(
            "/mfa/backup-codes/regenerate",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        event.remove(client.Session.class_, "before_flush", _fail_audit_inserts)

    assert resp.status_code == 503
    with client.Session() as s:
        rows = s.query(app_mod.MfaBackupCode).filter_by(user_id=user_id).all()
        assert len(rows) == 10
        assert all(row.used_at is None and row.invalidated_at is None for row in rows)


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
