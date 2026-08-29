"""W10 Final Stage 1 — services/gateway/app.py::require_session.

A live Redis session used to be authoritative on its own (idle TTL +
absolute lifetime cap, but nothing that looked back at the account). This
proves the new per-request revalidation: require_session now re-reads the
account and compares is_active/security_version (migration 034) against
what the session was issued with, so disabling an account or bumping its
security_version (the same thing roster_migrate.py does on a role change)
kills an already-issued session immediately rather than waiting for the
TTL — while a session nothing changed about keeps working, and one user's
change never touches another user's live session.

/mfa/status is used as the protected route under test: it's gated on plain
require_session with no downstream httpx fan-out, so a passing/failing
response is exactly require_session's own decision.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_session_revocation")


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


@pytest.fixture
def env(monkeypatch):
    fake = _FakeRedis()
    # create_session/get_session close over security.py's own module-level
    # _redis — patch it via the imported function's __globals__, the same
    # pattern the existing MFA route tests already use.
    monkeypatch.setitem(app_mod.create_session.__globals__, "_redis", lambda: fake)

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    app_mod.User.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def fake_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_db

    with Session() as s:
        s.add(app_mod.User(id=1, username="drnguyen", password_hash="x",
                            role="clinician", is_active=True, security_version=0))
        s.add(app_mod.User(id=2, username="drkim", password_hash="x",
                            role="clinician", is_active=True, security_version=0))
        s.commit()

    client = TestClient(app_mod.app)
    yield client, Session
    app_mod.app.dependency_overrides.clear()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_a_session_nothing_changed_about_keeps_working(env):
    client, Session = env
    with Session() as s:
        user = s.get(app_mod.User, 1)
        token = app_mod.create_session(user.id, user.username, user.role, user.security_version)

    resp = client.get("/mfa/status", headers=_auth(token))
    assert resp.status_code == 200


def test_disabling_the_account_kills_an_already_issued_session(env):
    client, Session = env
    with Session() as s:
        user = s.get(app_mod.User, 1)
        token = app_mod.create_session(user.id, user.username, user.role, user.security_version)

    assert client.get("/mfa/status", headers=_auth(token)).status_code == 200

    with Session() as s:
        user = s.get(app_mod.User, 1)
        user.is_active = False
        user.security_version += 1
        s.commit()

    resp = client.get("/mfa/status", headers=_auth(token))
    assert resp.status_code == 401


def test_a_role_downgrades_security_version_bump_kills_the_session(env):
    """roster_migrate.py bumps security_version on every migrate/deactivate
    write, independent of is_active — this proves the version comparison
    alone (not just the is_active check) invalidates a stale session, the
    case a role change without a status change would hit."""
    client, Session = env
    with Session() as s:
        user = s.get(app_mod.User, 1)
        token = app_mod.create_session(user.id, user.username, user.role, user.security_version)

    with Session() as s:
        user = s.get(app_mod.User, 1)
        user.role = "front_desk"
        user.security_version += 1
        s.commit()

    resp = client.get("/mfa/status", headers=_auth(token))
    assert resp.status_code == 401


def test_one_accounts_change_does_not_affect_another_users_live_session(env):
    client, Session = env
    with Session() as s:
        u1 = s.get(app_mod.User, 1)
        u2 = s.get(app_mod.User, 2)
        token1 = app_mod.create_session(u1.id, u1.username, u1.role, u1.security_version)
        token2 = app_mod.create_session(u2.id, u2.username, u2.role, u2.security_version)

    with Session() as s:
        user = s.get(app_mod.User, 1)
        user.is_active = False
        user.security_version += 1
        s.commit()

    assert client.get("/mfa/status", headers=_auth(token1)).status_code == 401
    assert client.get("/mfa/status", headers=_auth(token2)).status_code == 200


def test_anonymous_caller_is_still_rejected(env):
    client, _ = env
    assert client.get("/mfa/status").status_code == 401
