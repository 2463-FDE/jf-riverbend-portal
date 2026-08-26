"""What an account the roster does not cover experiences at login.

Branch 9 part 2. The client specified one message verbatim — "access is being
updated, contact your supervisor" — and the interesting part is not showing it,
it is showing it WITHOUT creating an account-existence oracle.

The check this replaced was `not user or not user.is_active or not
verify_password(...)`: correct, but a single indistinguishable 401. Adding a
distinct message to that shape would have let anyone probe usernames and learn
which accounts the roster does not cover, with no password. So the password is
verified FIRST and status second, and these tests pin that ordering.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_unmapped")

COPY = "access is being updated, contact your supervisor"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def env(monkeypatch):
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
    monkeypatch.setattr(app_mod, "verify_password", lambda raw, _hash: raw == PASSWORD)
    monkeypatch.setattr(app_mod, "create_session", lambda *a, **k: "tok")

    with Session() as s:
        s.add(app_mod.User(id=1, username="active_user", password_hash="h",
                           role="front_desk", is_active=True))
        s.add(app_mod.User(id=2, username="unmapped_user", password_hash="h",
                           role="staff", is_active=False,
                           disabled_reason="role_migration_unmapped"))
        s.add(app_mod.User(id=3, username="shared_login", password_hash="h",
                           role="staff", is_active=False,
                           disabled_reason="role_migration_no_owner"))
        s.add(app_mod.User(id=4, username="ordinary_disabled", password_hash="h",
                           role="staff", is_active=False, disabled_reason=None))
        # P1 identity foundation: an ACTIVE account whose role config/roles.yaml
        # does not define — role drift, a direct DB edit, a role renamed out of
        # the grid. Real roles.yaml is read (not mocked), so this is denied by
        # the actual current grid, not a fixture standing in for it.
        s.add(app_mod.User(id=5, username="drifted_role_user", password_hash="h",
                           role="obsolete_role_nobody_defines", is_active=True))
        s.commit()

    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def _login(client, username, password=PASSWORD):
    return client.post("/login", json={"username": username, "password": password})


def test_an_unmapped_account_gets_the_clients_copy_verbatim(env):
    r = _login(env, "unmapped_user")

    assert r.status_code == 403
    assert r.json()["detail"] == COPY


def test_a_disabled_shared_login_gets_the_same_copy(env):
    # frontdesk/labtech/itadmin are deactivated as no-owner. The person typing
    # is a real employee whose login was split, and they need to know to ask
    # rather than to keep retrying.
    r = _login(env, "shared_login")

    assert r.status_code == 403
    assert r.json()["detail"] == COPY


def test_the_copy_is_never_shown_without_the_correct_password(env):
    """The oracle test. Without this, the message tells an anonymous prober
    which usernames exist AND are unmapped."""
    r = _login(env, "unmapped_user", password="wrong")

    assert r.status_code == 401
    assert r.json()["detail"] == "invalid username or password"
    assert COPY not in r.text


def test_an_unknown_username_is_indistinguishable_from_a_wrong_password(env):
    unknown = _login(env, "no_such_user")
    wrong = _login(env, "active_user", password="wrong")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_an_ordinarily_disabled_account_keeps_the_generic_response(env):
    # Only the roster migration's own reasons get the new copy. An account
    # disabled for any other purpose must not start leaking why.
    r = _login(env, "ordinary_disabled")

    assert r.status_code == 401
    assert r.json()["detail"] == "invalid username or password"


def test_an_active_account_still_logs_in(env):
    # The regression that matters: reordering the checks must not break login.
    r = _login(env, "active_user")

    assert r.status_code == 200
    assert r.json()["token"] == "tok"


# --- P1 identity foundation: an active account with an unrecognized role ----


def test_an_active_account_with_an_undefined_role_gets_no_session(env):
    r = _login(env, "drifted_role_user")

    assert r.status_code == 401
    assert r.json()["detail"] == "invalid username or password"


def test_the_undefined_role_denial_is_never_shown_without_the_correct_password(env):
    # Same oracle concern as the role_migration_* case: without the password
    # check first, this would tell an anonymous prober that a role-config
    # problem exists for this specific username.
    r = _login(env, "drifted_role_user", password="wrong")

    assert r.status_code == 401
    assert r.json()["detail"] == "invalid username or password"


def test_an_undefined_role_denial_is_indistinguishable_from_any_other_401(env):
    drifted = _login(env, "drifted_role_user")
    unknown = _login(env, "no_such_user")
    wrong_password = _login(env, "active_user", password="wrong")

    assert drifted.status_code == unknown.status_code == wrong_password.status_code == 401
    assert drifted.json()["detail"] == unknown.json()["detail"] == wrong_password.json()["detail"]


def test_an_undefined_role_denial_is_logged(env, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        _login(env, "drifted_role_user")

    assert any(
        "not defined in roles.yaml" in m and "drifted_role_user" in m
        for m in caplog.messages
    )


def test_every_occurrence_is_logged_so_the_list_can_go_to_the_client(env, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        _login(env, "unmapped_user")

    assert any("unmapped account" in m and "unmapped_user" in m for m in caplog.messages)
