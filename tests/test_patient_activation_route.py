"""Issuance and activation at the route level, against in-memory SQLite.

The property that matters most here is that activation is not an oracle: every
failure must look identical from outside, or the endpoint becomes a way to
discover which codes exist.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_activation")
inv = app_mod.patient_invitations

VALID = "valid-token-abc"
TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


@pytest.fixture
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    app_mod.User.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS patient_access_grants ("
                       "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, patient_id INTEGER, "
                       "granted_at TEXT, revoked_at TEXT, expires_at TEXT, UNIQUE(user_id, patient_id))"))
    Session = sessionmaker(bind=engine)

    def fake_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_db
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)
    monkeypatch.setattr(app_mod, "get_session",
                        lambda t: {"user_id": "2", "username": "frontdesk", "role": "front_desk"} if t == VALID else None)
    client = TestClient(app_mod.app)
    with Session() as s:
        s.add(app_mod.User(id=2, username="frontdesk", password_hash="x", role="front_desk"))
        # The authoritative patient row activation now reads full_name from,
        # and the active grant issuance now requires of its caller — both
        # added so issuance/activation match the client's least-privilege and
        # identity requirements rather than trusting patients.write alone.
        s.add(app_mod.Patient(id=1042, name="Maria Gonzalez"))
        s.add(app_mod.PatientAccessGrant(user_id=2, patient_id=1042))
        s.commit()
    yield client, Session
    app_mod.app.dependency_overrides.clear()


def _auth():
    return {"Authorization": f"Bearer {VALID}"}


# --- issuance --------------------------------------------------------------


def test_front_desk_can_issue_an_invitation(env):
    client, _ = env
    resp = client.post("/patients/1042/invitation", headers=_auth())

    assert resp.status_code == 201
    assert resp.json()["patient_id"] == 1042
    assert resp.json()["code"]


def test_the_code_is_returned_once_and_never_stored(env):
    client, Session = env
    code = client.post("/patients/1042/invitation", headers=_auth()).json()["code"]

    with Session() as s:
        row = s.execute(text("SELECT code_hash FROM patient_invitations")).one()
    # The stored value is a hash, and the code cannot be read back out of it.
    assert inv.normalise_code(code) not in row[0]
    assert inv.codes_match(code, row[0])


def test_a_role_without_patients_write_cannot_issue(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(app_mod, "get_session",
                        lambda t: {"user_id": "2", "username": "doc", "role": "clinician"} if t == VALID else None)

    assert client.post("/patients/1042/invitation", headers=_auth()).status_code == 403


def test_an_anonymous_caller_cannot_issue(env):
    client, _ = env
    assert client.post("/patients/1042/invitation").status_code == 401


# --- activation ------------------------------------------------------------


def test_activation_creates_the_account_and_exactly_one_grant(env):
    client, Session = env
    code = client.post("/patients/1042/invitation", headers=_auth()).json()["code"]

    resp = client.post("/patient/activate", json={"code": code, "password": "a-long-passphrase"})

    assert resp.status_code == 200
    assert resp.json()["username"] == "patient-1042"
    with Session() as s:
        account = s.execute(
            text("SELECT id, role, patient_id, is_active, full_name FROM users WHERE username='patient-1042'")
        ).one()
        assert account[1] == "patient"      # no staff permission whatsoever
        assert account[2] == 1042
        # The root cause this fixes: full_name used to be left NULL — nothing
        # ever populated it, so the account existed and could sign in, but
        # every screen that displays a patient's own identity had nothing to
        # show. It is read from `patients.name`, the authoritative source, not
        # supplied by the caller anywhere in this flow.
        assert account[4] == "Maria Gonzalez"
        grants = s.execute(text("SELECT patient_id FROM patient_access_grants WHERE user_id=:u"),
                           {"u": account[0]}).all()
    # THE grant: one row, their own chart. All scoping flows from this through
    # the same gate that scopes staff — no patient-specific authorization path.
    assert [g[0] for g in grants] == [1042]


def test_activation_fails_closed_when_the_patient_row_no_longer_exists(env):
    """An invitation's patient_id resolving to nothing is not a lookup gap the
    caller should be able to turn into an account — same generic refusal as
    every other activation failure, so this is not an oracle either."""
    client, Session = env
    code = client.post("/patients/1042/invitation", headers=_auth()).json()["code"]
    with Session() as s:
        s.execute(text("DELETE FROM patients WHERE id = 1042"))
        s.commit()

    resp = client.post("/patient/activate", json={"code": code, "password": "a-long-passphrase"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid or expired invitation code"
    with Session() as s:
        assert s.execute(text("SELECT count(*) FROM users WHERE username='patient-1042'")).scalar() == 0


def test_issuing_requires_an_active_grant_for_this_patient_not_just_the_role(env):
    """patients.write is a role-wide permission; it says nothing about THIS
    chart. The patient exists — so a 403 here can only be the grant check,
    isolated from the existence check `test_a_nonexistent_patient_...` below
    covers — but front desk holds no grant for it."""
    client, Session = env
    with Session() as s:
        s.add(app_mod.Patient(id=1043, name="James O'Brien"))
        s.commit()

    resp = client.post("/patients/1043/invitation", headers=_auth())

    assert resp.status_code == 403


def test_a_nonexistent_patient_cannot_receive_an_invitation(env):
    client, _ = env
    assert client.post("/patients/999999/invitation", headers=_auth()).status_code == 404


def test_an_active_account_blocks_a_second_invitation(env):
    client, Session = env
    code = client.post("/patients/1042/invitation", headers=_auth()).json()["code"]
    client.post("/patient/activate", json={"code": code, "password": "a-long-passphrase"})

    resp = client.post("/patients/1042/invitation", headers=_auth())

    assert resp.status_code == 409
    # Machine-readable, not English text the frontend would have to parse —
    # and specifically ACTIVE_PORTAL_ACCOUNT, not LIVE_INVITATION: the two
    # conflicts need different UI (no revoke control makes sense for an
    # account that already exists).
    assert resp.json()["detail"]["reason"] == "ACTIVE_PORTAL_ACCOUNT"


def test_revoking_requires_the_same_active_grant_as_issuing(env, monkeypatch):
    """Revoking had lagged issuing's own has_active_grant requirement — a
    caller who cannot issue for this chart must not be able to revoke for it
    either."""
    client, Session = env
    client.post("/patients/1042/invitation", headers=_auth())
    monkeypatch.setattr(
        app_mod, "get_session",
        lambda t: {"user_id": "3", "username": "ungranted", "role": "front_desk"} if t == VALID else None,
    )
    with Session() as s:
        s.add(app_mod.User(id=3, username="ungranted", password_hash="x", role="front_desk"))
        s.commit()

    resp = client.delete("/patients/1042/invitation", headers=_auth())

    assert resp.status_code == 403
    with Session() as s:
        still_live = s.execute(
            text("SELECT revoked_at FROM patient_invitations WHERE patient_id = 1042")
        ).scalar()
        assert still_live is None, "an unauthorized caller must not revoke"


def test_revoking_a_live_invitation_allows_a_replacement_to_be_issued(env):
    client, _ = env
    client.post("/patients/1042/invitation", headers=_auth())

    revoked = client.delete("/patients/1042/invitation", headers=_auth())
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] == 1

    reissued = client.post("/patients/1042/invitation", headers=_auth())
    assert reissued.status_code == 201
    assert reissued.json()["code"]


def test_a_code_cannot_be_redeemed_twice(env):
    client, _ = env
    code = client.post("/patients/1042/invitation", headers=_auth()).json()["code"]
    first = client.post("/patient/activate", json={"code": code, "password": "a-long-passphrase"})
    second = client.post("/patient/activate", json={"code": code, "password": "another-passphrase"})

    assert first.status_code == 200
    assert second.status_code == 400


@pytest.mark.parametrize("bad", ["ABCD-EFGH-JKMN-PQRS", "", "not-a-code"])
def test_an_unknown_code_is_refused(env, bad):
    client, _ = env
    assert client.post("/patient/activate", json={"code": bad, "password": "a-long-passphrase"}).status_code == 400


def test_activation_is_not_an_oracle(env):
    """Every failure looks identical from outside.

    If "expired" read differently from "unknown", the endpoint would confirm
    which codes exist and turn into a way to discover them.
    """
    client, Session = env
    code = client.post("/patients/1042/invitation", headers=_auth()).json()["code"]
    with Session() as s:  # expire it
        s.execute(text("UPDATE patient_invitations SET expires_at = '2000-01-01 00:00:00'"))
        s.commit()

    expired = client.post("/patient/activate", json={"code": code, "password": "a-long-passphrase"})
    unknown = client.post("/patient/activate", json={"code": "ABCD-EFGH-JKMN-PQRS", "password": "a-long-passphrase"})

    assert expired.status_code == unknown.status_code == 400
    assert expired.json() == unknown.json()  # byte-identical, no reason leaked


def test_a_short_password_is_refused_before_the_code_is_consumed(env):
    client, _ = env
    code = client.post("/patients/1042/invitation", headers=_auth()).json()["code"]

    weak = client.post("/patient/activate", json={"code": code, "password": "short"})
    assert weak.status_code == 400
    assert "at least" in weak.json()["detail"]

    # The invitation must survive a rejected password, or one typo burns it.
    assert client.post("/patient/activate", json={"code": code, "password": "a-long-passphrase"}).status_code == 200


def test_the_activated_account_can_sign_in(env, monkeypatch):
    # Session minting is stubbed — Redis mechanics are covered in
    # test_gateway_security.py; what matters here is that the account created
    # by activation is a real, sign-in-able credential.
    monkeypatch.setattr(app_mod, "create_session", lambda uid, uname, role: f"session-{uid}")
    client, _ = env
    code = client.post("/patients/1042/invitation", headers=_auth()).json()["code"]
    client.post("/patient/activate", json={"code": code, "password": "a-long-passphrase"})

    resp = client.post("/login", json={"username": "patient-1042", "password": "a-long-passphrase"})

    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == "patient"
