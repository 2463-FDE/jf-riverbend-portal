"""Week 4 catch-up — services/records-service/patient_access_gate.py, unit
level, against a REAL in-memory SQLite database (not a hand-rolled fake).

Exercises the actual query the authorization boundary runs — correct SQL
semantics for "active grant": revoked/expired handling, the users.is_active
join, and the batch multi-candidate check. PR #23 review round 2 (2026-08-07):
grants are keyed on the stable users.id (never username), and a grant is only
honored while its user is still active — both proven here against a real
`users` table (the records-service `User` model).
"""
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from conftest import load_module

pag_mod = load_module(
    "services/records-service/patient_access_gate.py", "records_patient_access_gate"
)

# See the long note in git history: the gate's own `from models import ...`
# already populated sys.modules["models"]/["db"], so importing them here returns
# the SAME classes/metadata the gate queries against.
import db as db_mod  # noqa: E402
import models as models_mod  # noqa: E402
from libs.patient_view_agent import Action, AuthorizationDenied, AuthorizationRequest, Purpose  # noqa: E402

SqlPatientAccessGate = pag_mod.SqlPatientAccessGate
authorized_patient_ids = pag_mod.authorized_patient_ids
Patient = models_mod.Patient
PatientAccessGrant = models_mod.PatientAccessGrant
User = models_mod.User
Base = db_mod.Base

# Stable users.id principals (X-Actor-Id is this id as a string).
FRONTDESK = 1  # active, holds grants
BILLING = 2    # active, holds no grants
DISABLED = 3   # holds a grant but is_active=False -> must be denied


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(User(id=FRONTDESK, username="frontdesk", is_active=True))
    db.add(User(id=BILLING, username="billing-clerk", is_active=True))
    db.add(User(id=DISABLED, username="disabled-doc", is_active=False))
    db.add(Patient(id=1042, name="Authorized Patient"))
    db.add(Patient(id=2001, name="Unrelated Patient"))
    db.commit()
    return db


def _grant(db, *, user_id, patient_id, revoked_at=None, expires_at=None):
    db.add(
        PatientAccessGrant(
            user_id=user_id, patient_id=patient_id, revoked_at=revoked_at, expires_at=expires_at
        )
    )
    db.commit()


def _request(actor_id, patient_id, correlation_id="corr-1"):
    return AuthorizationRequest(
        actor_id=actor_id,
        patient_id=patient_id,
        action=Action.VIEW_PATIENT_CHART,
        purpose=Purpose.TREATMENT,
        correlation_id=correlation_id,
    )


# --- authorized access ------------------------------------------------------


def test_authorized_actor_can_view_the_requested_chart():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)

    scope = SqlPatientAccessGate(db).authorize(_request(str(FRONTDESK), 1042))

    assert scope.actor_id == str(FRONTDESK)
    assert scope.patient_id == 1042


# --- unrelated/unauthorized/invalid actors are denied -----------------------


def test_unrelated_patient_is_denied():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)  # grant exists, but not for 2001

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request(str(FRONTDESK), 2001))

    assert exc.value.denial.reason.value == "not_authorized"


def test_actor_with_no_grants_at_all_is_denied():
    db = _fresh_session()

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request(str(BILLING), 1042))

    assert exc.value.denial.reason.value == "not_authorized"


def test_missing_actor_is_denied():
    db = _fresh_session()

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request("", 1042))

    assert exc.value.denial.reason.value == "unknown_actor"


def test_non_numeric_actor_is_denied():
    # X-Actor-Id must be a users.id; a non-numeric value (e.g. a leftover
    # username) is not a valid principal (PR #23 review round 2).
    db = _fresh_session()

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request("frontdesk", 1042))

    assert exc.value.denial.reason.value == "unknown_actor"


def test_grant_for_a_disabled_user_is_denied():
    # Finding 3b: a disabled account must not retain chart access through an
    # existing grant — the gate joins users.is_active.
    db = _fresh_session()
    _grant(db, user_id=DISABLED, patient_id=1042)

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request(str(DISABLED), 1042))

    assert exc.value.denial.reason.value == "not_authorized"


# --- revoked / expired grants are denied, distinctly from "never granted" --


def test_revoked_grant_is_denied():
    db = _fresh_session()
    _grant(
        db,
        user_id=FRONTDESK,
        patient_id=1042,
        revoked_at=datetime.datetime.now(datetime.timezone.utc),
    )

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request(str(FRONTDESK), 1042))

    assert exc.value.denial.reason.value == "not_authorized"


def test_expired_grant_is_denied():
    db = _fresh_session()
    _grant(
        db,
        user_id=FRONTDESK,
        patient_id=1042,
        expires_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1),
    )

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request(str(FRONTDESK), 1042))

    assert exc.value.denial.reason.value == "not_authorized"


def test_grant_with_a_future_expiry_still_allows():
    db = _fresh_session()
    _grant(
        db,
        user_id=FRONTDESK,
        patient_id=1042,
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
    )

    scope = SqlPatientAccessGate(db).authorize(_request(str(FRONTDESK), 1042))

    assert scope.patient_id == 1042


# --- no patient-existence oracle --------------------------------------------


def test_denial_reason_is_identical_for_an_existing_vs_nonexistent_patient():
    # authorize() never queries `patients` — a patient_id with no grant row
    # is denied identically whether or not it exists in `patients` at all.
    db = _fresh_session()

    with pytest.raises(AuthorizationDenied) as exc_existing:
        SqlPatientAccessGate(db).authorize(_request(str(FRONTDESK), 1042))  # exists, no grant
    with pytest.raises(AuthorizationDenied) as exc_missing:
        SqlPatientAccessGate(db).authorize(_request(str(FRONTDESK), 999999))  # does not exist

    assert (
        exc_existing.value.denial.reason.value
        == exc_missing.value.denial.reason.value
        == "not_authorized"
    )


# --- database/policy failure denies closed ----------------------------------


def test_grant_lookup_db_failure_denies_closed(monkeypatch):
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)

    def _boom(*_a, **_kw):
        raise SQLAlchemyError("simulated connection drop")

    monkeypatch.setattr(db, "execute", _boom)

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request(str(FRONTDESK), 1042))

    assert exc.value.denial.reason.value == "policy_error"


# --- batch check: reconciliation-shaped multi-candidate exclusion -----------


def test_authorized_patient_ids_returns_only_the_granted_subset():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)
    # No grant for 2001 — it exists in `patients` but frontdesk cannot see it.

    allowed = authorized_patient_ids(db, str(FRONTDESK), [1042, 2001])

    assert allowed == {1042}


def test_authorized_patient_ids_excludes_revoked_and_expired_grants():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)
    _grant(
        db,
        user_id=FRONTDESK,
        patient_id=2001,
        revoked_at=datetime.datetime.now(datetime.timezone.utc),
    )

    allowed = authorized_patient_ids(db, str(FRONTDESK), [1042, 2001])

    assert allowed == {1042}


def test_authorized_patient_ids_excludes_a_disabled_users_grants():
    db = _fresh_session()
    _grant(db, user_id=DISABLED, patient_id=1042)

    assert authorized_patient_ids(db, str(DISABLED), [1042]) == set()


def test_authorized_patient_ids_is_empty_for_an_unknown_actor():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)

    assert authorized_patient_ids(db, "", [1042]) == set()
    assert authorized_patient_ids(db, "999", [1042]) == set()  # no such user
    assert authorized_patient_ids(db, "frontdesk", [1042]) == set()  # non-numeric


def test_authorized_patient_ids_fails_closed_on_db_error(monkeypatch):
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)

    def _boom(*_a, **_kw):
        raise SQLAlchemyError("simulated connection drop")

    monkeypatch.setattr(db, "execute", _boom)

    assert authorized_patient_ids(db, str(FRONTDESK), [1042]) == set()
