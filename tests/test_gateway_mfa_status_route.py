"""GET /mfa/status (services/gateway/app.py)."""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_mfa_status")

PASSWORD = "portal123-testpass"


class _FakeRedis:
    def __init__(self):
        self._store = {}

    def hset(self, key, mapping=None):
        self._store[key] = dict(mapping)

    def hgetall(self, key):
        return dict(self._store.get(key, {}))

    def expire(self, key, ttl):
        pass

    def delete(self, key):
        self._store.pop(key, None)

    def incr(self, key):
        return 1


@pytest.fixture
def client(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setitem(app_mod.create_mfa_challenge.__globals__, "_redis", lambda: fake)

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    app_mod.User.metadata.create_all(engine)
    app_mod.MfaBackupCode.metadata.create_all(engine)
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


def test_status_reports_unenrolled_pilot_account(client, monkeypatch):
    monkeypatch.setattr(app_mod.mfa_config, "mfa_requirement_for", lambda user, **kw: "prompt")
    with client.Session() as s:
        user = app_mod.User(username="drnguyen", password_hash=app_mod.hash_password(PASSWORD),
                             role="clinician", is_active=True, mfa_pilot=True, mfa_shared_account=False)
        s.add(user)
        s.commit()
        s.refresh(user)
        user_id = user.id
    token = app_mod.create_session(user_id, "drnguyen", "clinician")

    resp = client.get("/mfa/status", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["requirement"] == "prompt"
    assert body["enrolled"] is False
    assert body["pilot"] is True
    assert body["shared_account"] is False
    assert body["backup_codes_remaining"] is None


def test_status_reports_backup_codes_remaining_once_enrolled(client, monkeypatch):
    monkeypatch.setattr(app_mod.mfa_config, "mfa_requirement_for", lambda user, **kw: "enforce")
    with client.Session() as s:
        user = app_mod.User(username="drnguyen", password_hash=app_mod.hash_password(PASSWORD),
                             role="clinician", is_active=True, mfa_enrolled_at=app_mod.func.now())
        s.add(user)
        s.commit()
        s.refresh(user)
        user_id = user.id
        for i in range(4):
            s.add(app_mod.MfaBackupCode(user_id=user_id, code_hash=app_mod.hash_password(f"C{i}")))
        s.commit()
    token = app_mod.create_session(user_id, "drnguyen", "clinician")

    resp = client.get("/mfa/status", headers={"Authorization": f"Bearer {token}"})

    assert resp.json()["backup_codes_remaining"] == 4


def test_status_requires_a_session(client):
    resp = client.get("/mfa/status")

    assert resp.status_code == 401


def test_new_accounts_default_to_shared_and_not_piloted_fail_closed(client):
    # Migration 033's fail-closed defaults, exercised through the ORM model
    # exactly the way a real INSERT (registration, seed generator, or the
    # roster) would leave them if the column is never set explicitly.
    with client.Session() as s:
        user = app_mod.User(username="brandnew", password_hash=app_mod.hash_password(PASSWORD),
                             role="clinician", is_active=True)
        s.add(user)
        s.commit()
        s.refresh(user)
        assert user.mfa_shared_account is True
        assert user.mfa_pilot is False
