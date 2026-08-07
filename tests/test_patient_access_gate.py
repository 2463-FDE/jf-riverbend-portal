"""Week 4 catch-up — services/records-service/patient_access_gate.py, unit
level, against a REAL in-memory SQLite database (not a hand-rolled fake).

Faking SQLAlchemy's `execute(select(...))` by hand (as the route-level test
files in this directory do) is fine for proving route WIRING/ordering, but
this file exercises the actual query the authorization boundary runs —
correct SQL semantics for "active grant" (revoked/expired handling, the
batch multi-candidate check) are exactly what a hand-rolled fake can't
prove. `patient_access_grants.username` FKs to `users.username`, a table
records-service doesn't model (it doesn't own that table) — a bare stub
`users` table is registered on the shared metadata purely so
`create_all()` can resolve the FK target; SQLite does not enforce foreign
keys by default, so this is DDL-only, not a behavioral stand-in for the
real `users` table.
"""
import datetime

import pytest
from sqlalchemy import Column, Table, Text, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from conftest import load_module

pag_mod = load_module(
    "services/records-service/patient_access_gate.py", "records_patient_access_gate"
)

# Deliberately NOT two more load_module() calls for models.py/db.py: each
# load_module() call execs its target under a fresh, uniquely-named module
# object, so a second independent load of models.py would produce a SEPARATE
# Patient/PatientAccessGrant class (and a separate `Base`, hence separate
# metadata) from the ones patient_access_gate.py actually queries against.
# By the time the load above finished, its own `from models import
# PatientAccessGrant` (and models.py's own `from db import Base`) already
# populated the plain sys.modules["models"]/["db"] entries and put
# services/records-service/ on sys.path — a normal `import` here returns
# those SAME cached modules, guaranteeing this file builds rows with the
# exact classes/metadata the gate under test uses.
import db as db_mod  # noqa: E402
import models as models_mod  # noqa: E402
from libs.patient_view_agent import Action, AuthorizationDenied, AuthorizationRequest, Purpose  # noqa: E402

SqlPatientAccessGate = pag_mod.SqlPatientAccessGate
authorized_patient_ids = pag_mod.authorized_patient_ids
Patient = models_mod.Patient
PatientAccessGrant = models_mod.PatientAccessGrant
Base = db_mod.Base

_USERS_STUB_REGISTERED = False


def _fresh_session():
    global _USERS_STUB_REGISTERED
    if not _USERS_STUB_REGISTERED:
        Table("users", Base.metadata, Column("username", Text, primary_key=True))
        _USERS_STUB_REGISTERED = True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    users_table = Base.metadata.tables["users"]
    for username in ("frontdesk", "billing-clerk"):
        db.execute(users_table.insert().values(username=username))
    db.add(Patient(id=1042, name="Authorized Patient"))
    db.add(Patient(id=2001, name="Unrelated Patient"))
    db.commit()
    return db


def _grant(db, *, username, patient_id, revoked_at=None, expires_at=None):
    db.add(
        PatientAccessGrant(
            username=username, patient_id=patient_id, revoked_at=revoked_at, expires_at=expires_at
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
    _grant(db, username="frontdesk", patient_id=1042)

    scope = SqlPatientAccessGate(db).authorize(_request("frontdesk", 1042))

    assert scope.actor_id == "frontdesk"
    assert scope.patient_id == 1042


# --- unrelated/unauthorized patient is denied -------------------------------


def test_unrelated_patient_is_denied():
    db = _fresh_session()
    _grant(db, username="frontdesk", patient_id=1042)  # grant exists, but not for 2001

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request("frontdesk", 2001))

    assert exc.value.denial.reason.value == "not_authorized"


def test_actor_with_no_grants_at_all_is_denied():
    db = _fresh_session()

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request("billing-clerk", 1042))

    assert exc.value.denial.reason.value == "not_authorized"


def test_missing_actor_is_denied():
    db = _fresh_session()

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request("", 1042))

    assert exc.value.denial.reason.value == "unknown_actor"


# --- revoked / expired grants are denied, distinctly from "never granted" --


def test_revoked_grant_is_denied():
    db = _fresh_session()
    _grant(
        db,
        username="frontdesk",
        patient_id=1042,
        revoked_at=datetime.datetime.now(datetime.timezone.utc),
    )

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request("frontdesk", 1042))

    assert exc.value.denial.reason.value == "not_authorized"


def test_expired_grant_is_denied():
    db = _fresh_session()
    _grant(
        db,
        username="frontdesk",
        patient_id=1042,
        expires_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1),
    )

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request("frontdesk", 1042))

    assert exc.value.denial.reason.value == "not_authorized"


def test_grant_with_a_future_expiry_still_allows():
    db = _fresh_session()
    _grant(
        db,
        username="frontdesk",
        patient_id=1042,
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
    )

    scope = SqlPatientAccessGate(db).authorize(_request("frontdesk", 1042))

    assert scope.patient_id == 1042


# --- no patient-existence oracle --------------------------------------------


def test_denial_reason_is_identical_for_an_existing_vs_nonexistent_patient():
    # authorize() never queries `patients` — a patient_id with no grant row
    # is denied identically whether or not it exists in `patients` at all.
    db = _fresh_session()

    with pytest.raises(AuthorizationDenied) as exc_existing:
        SqlPatientAccessGate(db).authorize(_request("frontdesk", 1042))  # exists, no grant
    with pytest.raises(AuthorizationDenied) as exc_missing:
        SqlPatientAccessGate(db).authorize(_request("frontdesk", 999999))  # does not exist at all

    assert exc_existing.value.denial.reason.value == exc_missing.value.denial.reason.value == "not_authorized"


# --- database/policy failure denies closed ----------------------------------


def test_grant_lookup_db_failure_denies_closed(monkeypatch):
    db = _fresh_session()
    _grant(db, username="frontdesk", patient_id=1042)

    def _boom(*_a, **_kw):
        raise SQLAlchemyError("simulated connection drop")

    monkeypatch.setattr(db, "execute", _boom)

    with pytest.raises(AuthorizationDenied) as exc:
        SqlPatientAccessGate(db).authorize(_request("frontdesk", 1042))

    assert exc.value.denial.reason.value == "policy_error"


# --- batch check: reconciliation-shaped multi-candidate exclusion -----------


def test_authorized_patient_ids_returns_only_the_granted_subset():
    # The reconciliation-shaped scenario: the requested patient is
    # authorized, a candidate matched by some other signal (e.g. SSN) is
    # not. The unauthorized candidate must be excluded from the returned
    # set — callers are required (see this module's docstring) to drop it
    # silently rather than surface it as a count or placeholder, so nothing
    # about its existence leaks.
    db = _fresh_session()
    _grant(db, username="frontdesk", patient_id=1042)
    # No grant for 2001 — it exists in `patients` but frontdesk cannot see it.

    allowed = authorized_patient_ids(db, "frontdesk", [1042, 2001])

    assert allowed == {1042}


def test_authorized_patient_ids_excludes_revoked_and_expired_grants():
    db = _fresh_session()
    _grant(db, username="frontdesk", patient_id=1042)
    _grant(
        db,
        username="frontdesk",
        patient_id=2001,
        revoked_at=datetime.datetime.now(datetime.timezone.utc),
    )

    allowed = authorized_patient_ids(db, "frontdesk", [1042, 2001])

    assert allowed == {1042}


def test_authorized_patient_ids_is_empty_for_an_unknown_actor():
    db = _fresh_session()
    _grant(db, username="frontdesk", patient_id=1042)

    assert authorized_patient_ids(db, "", [1042]) == set()
    assert authorized_patient_ids(db, "nobody", [1042]) == set()


def test_authorized_patient_ids_fails_closed_on_db_error(monkeypatch):
    db = _fresh_session()
    _grant(db, username="frontdesk", patient_id=1042)

    def _boom(*_a, **_kw):
        raise SQLAlchemyError("simulated connection drop")

    monkeypatch.setattr(db, "execute", _boom)

    assert authorized_patient_ids(db, "frontdesk", [1042]) == set()
