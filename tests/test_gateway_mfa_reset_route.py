"""POST /mfa/reset (services/gateway/app.py) — the supervisor-authorized
reset flow.

Covers: unauthorized (wrong permission) and self-approved resets are both
denied, a real supervisor reset succeeds and clears enrollment/backup codes
atomically, and the audit trail records the action without ever carrying
secret material.
"""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_mfa_reset")

PASSWORD = "portal123-testpass"


class _FakeRedis:
    def __init__(self):
        self._store = {}
        self._counters = {}

    def hset(self, key, mapping=None):
        self._store[key] = dict(mapping)

    def hgetall(self, key):
        return dict(self._store.get(key, {}))

    def expire(self, key, ttl):
        pass

    def delete(self, key):
        self._store.pop(key, None)

    def incr(self, key):
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MFA_ACTIVE_KEY_VERSION", "v1")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY_V1", base64.b64encode(b"\x04" * 32).decode())
    app_mod.mfa_crypto.reset_key_provider()

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
    yield tc
    app_mod.app.dependency_overrides.clear()


def _make_enrolled_user(client, *, username, role="clinician"):
    secret = "JBSWY3DPEHPK3PXP"
    with client.Session() as s:
        user = app_mod.User(
            username=username, password_hash=app_mod.hash_password(PASSWORD),
            role=role, is_active=True,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        envelope, version = app_mod.mfa_crypto.encrypt_totp_secret(user.id, secret)
        user.mfa_secret_ciphertext = envelope
        user.mfa_secret_key_version = version
        user.mfa_enrolled_at = app_mod.func.now()
        for i in range(10):
            s.add(app_mod.MfaBackupCode(user_id=user.id, code_hash=app_mod.hash_password(f"CODE{i}")))
        s.commit()
        return user.id


def _session_for(user_id, username, role):
    return app_mod.create_session(user_id, username, role)


def test_reset_requires_accounts_write_permission(client):
    target_id = _make_enrolled_user(client, username="target")
    # front_desk holds neither accounts.write nor accounts.read.
    actor_id = target_id + 1
    with client.Session() as s:
        s.add(app_mod.User(id=actor_id, username="frontdesk1", password_hash=app_mod.hash_password(PASSWORD),
                            role="front_desk", is_active=True))
        s.commit()
    token = _session_for(actor_id, "frontdesk1", "front_desk")

    resp = client.post("/mfa/reset", json={"username": "target"}, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403


def test_self_reset_is_denied_even_for_an_it_admin(client):
    admin_id = _make_enrolled_user(client, username="itadmin1", role="it_admin")
    token = _session_for(admin_id, "itadmin1", "it_admin")

    resp = client.post("/mfa/reset", json={"username": "itadmin1"}, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403
    with client.Session() as s:
        user = s.get(app_mod.User, admin_id)
        assert user.mfa_enrolled_at is not None  # untouched


def test_supervisor_reset_succeeds_and_clears_everything_atomically(client):
    target_id = _make_enrolled_user(client, username="target")
    admin_id = target_id + 100
    with client.Session() as s:
        s.add(app_mod.User(id=admin_id, username="itadmin2", password_hash=app_mod.hash_password(PASSWORD),
                            role="it_admin", is_active=True))
        s.commit()
    token = _session_for(admin_id, "itadmin2", "it_admin")

    resp = client.post("/mfa/reset", json={"username": "target"}, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "reset", "username": "target"}

    with client.Session() as s:
        user = s.get(app_mod.User, target_id)
        assert user.mfa_secret_ciphertext is None
        assert user.mfa_secret_key_version is None
        assert user.mfa_enrolled_at is None
        assert user.mfa_last_totp_step is None
        assert user.mfa_challenge_epoch == 1

        active_codes = (
            s.query(app_mod.MfaBackupCode)
            .filter_by(user_id=target_id, used_at=None, invalidated_at=None)
            .count()
        )
        assert active_codes == 0


def test_reset_invalidates_every_pre_reset_login_challenge(client, monkeypatch):
    monkeypatch.setattr(app_mod.mfa_config, "mfa_requirement_for", lambda user, **kwargs: "enforce")
    target_id = _make_enrolled_user(client, username="target")
    admin_id = target_id + 150
    with client.Session() as s:
        target = s.get(app_mod.User, target_id)
        old_epoch = target.mfa_challenge_epoch
        s.add(
            app_mod.User(
                id=admin_id,
                username="itadmin-epoch",
                password_hash=app_mod.hash_password(PASSWORD),
                role="it_admin",
                is_active=True,
            )
        )
        s.commit()

    stale_challenge = app_mod.create_mfa_challenge(
        target_id,
        purpose="login",
        mfa_epoch=old_epoch,
    )
    admin_token = _session_for(admin_id, "itadmin-epoch", "it_admin")
    reset = client.post(
        "/mfa/reset",
        json={"username": "target"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert reset.status_code == 200

    stale = client.post("/mfa/enroll/start", json={"challenge_token": stale_challenge})
    assert stale.status_code == 401

    # The write boundary carries the same epoch predicate, closing the race
    # where reset commits after initial challenge resolution but before the
    # pending secret is stored.
    with client.Session() as s:
        assert app_mod._store_pending_mfa_secret(
            s,
            user_id=target_id,
            expected_challenge_epoch=old_epoch,
            ciphertext="stale-ciphertext",
            key_version="v1",
        ) is False
        s.rollback()
        target = s.get(app_mod.User, target_id)
        current_epoch = target.mfa_challenge_epoch
        assert target.mfa_secret_ciphertext is None

    # A new password proof would receive the current epoch and remains able
    # to start the required re-enrollment flow.
    fresh_challenge = app_mod.create_mfa_challenge(
        target_id,
        purpose="login",
        mfa_epoch=current_epoch,
    )
    fresh = client.post("/mfa/enroll/start", json={"challenge_token": fresh_challenge})
    assert fresh.status_code == 200


def test_reset_is_audited_without_any_secret_material(client):
    target_id = _make_enrolled_user(client, username="target")
    admin_id = target_id + 200
    with client.Session() as s:
        s.add(app_mod.User(id=admin_id, username="itadmin3", password_hash=app_mod.hash_password(PASSWORD),
                            role="it_admin", is_active=True))
        s.commit()
    token = _session_for(admin_id, "itadmin3", "it_admin")

    client.post("/mfa/reset", json={"username": "target"}, headers={"Authorization": f"Bearer {token}"})

    with client.Session() as s:
        rows = s.query(app_mod.AuditLog).filter(app_mod.AuditLog.message.like("mfa_reset%")).all()
        assert len(rows) == 1
        assert "target_username=target" in rows[0].message
        assert rows[0].actor == "itadmin3"
        # No secret, code, or key material of any kind in the row.
        for forbidden in ("JBSWY3DPEHPK3PXP", "CODE0", "pbkdf2_sha256"):
            assert forbidden not in rows[0].message


def test_reset_of_a_nonexistent_account_is_404(client):
    admin_id = 1
    with client.Session() as s:
        s.add(app_mod.User(id=admin_id, username="itadmin4", password_hash=app_mod.hash_password(PASSWORD),
                            role="it_admin", is_active=True))
        s.commit()
    token = _session_for(admin_id, "itadmin4", "it_admin")

    resp = client.post("/mfa/reset", json={"username": "ghost"}, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 404


def test_reset_is_idempotent_for_an_account_with_no_active_enrollment(client):
    with client.Session() as s:
        target = app_mod.User(username="neverenrolled", password_hash=app_mod.hash_password(PASSWORD),
                               role="clinician", is_active=True)
        s.add(target)
        s.commit()
        s.refresh(target)
        admin = app_mod.User(username="itadmin5", password_hash=app_mod.hash_password(PASSWORD),
                              role="it_admin", is_active=True)
        s.add(admin)
        s.commit()
        s.refresh(admin)
    token = _session_for(admin.id, "itadmin5", "it_admin")

    resp = client.post(
        "/mfa/reset", json={"username": "neverenrolled"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
