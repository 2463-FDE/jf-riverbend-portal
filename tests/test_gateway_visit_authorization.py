"""Stage 2 (feature-readiness) — services/gateway/visit_authorization.py,
unit level, against a REAL in-memory SQLite database (mirrors
tests/test_patient_access_gate.py's approach for the identical
patient_access_grants query shape, rather than a hand-rolled fake).

Exercises the actual query the gateway's /visits/{visit_id}/messages route
now runs before proxying anything downstream: an appointment is only
"authorized" for a user_id holding an active, non-expired grant for its
patient, joined to a still-active user — and never a source of a
patient/appointment existence oracle (a denial looks identical whether the
appointment doesn't exist, the user has no grant, or the grant's user is
disabled).
"""
import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from conftest import load_module

va_mod = load_module("services/gateway/visit_authorization.py", "gateway_visit_authorization")

# va_mod's own `from models import ...` already populated sys.modules
# ["models"]/["db"] — importing them here returns the SAME classes/metadata
# the module under test queries against (mirrors test_patient_access_gate.py).
import db as db_mod  # noqa: E402
import models as models_mod  # noqa: E402

find_authorized_appointment = va_mod.find_authorized_appointment
latest_insurance_member_id = va_mod.latest_insurance_member_id
parse_user_id = va_mod.parse_user_id
Appointment = models_mod.Appointment
PatientAccessGrant = models_mod.PatientAccessGrant
InsuranceCoverage = models_mod.InsuranceCoverage
User = models_mod.User
Base = db_mod.Base

FRONTDESK = 1  # active, holds a grant for patient 1042
BILLING = 2    # active, holds no grants
DISABLED = 3   # holds a grant for patient 1042 but is_active=False


def _fresh_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(User(id=FRONTDESK, username="frontdesk", password_hash="x", is_active=True, role="staff"))
    db.add(User(id=BILLING, username="billing-clerk", password_hash="x", is_active=True, role="staff"))
    db.add(User(id=DISABLED, username="disabled-doc", password_hash="x", is_active=False, role="staff"))
    db.add(Appointment(id=501, patient_id=1042))
    db.add(Appointment(id=502, patient_id=2001))  # a real appointment FRONTDESK has no grant for
    db.commit()
    return db


def _grant(db, *, user_id, patient_id, revoked_at=None, expires_at=None):
    db.add(PatientAccessGrant(user_id=user_id, patient_id=patient_id, revoked_at=revoked_at, expires_at=expires_at))
    db.commit()


# --- find_authorized_appointment: the core no-existence-oracle gate --------


def test_active_grant_authorizes_the_appointment():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)

    appointment = find_authorized_appointment(db, user_id=FRONTDESK, appointment_id=501)

    assert appointment is not None
    assert appointment.patient_id == 1042


def test_no_grant_at_all_returns_none():
    db = _fresh_session()

    assert find_authorized_appointment(db, user_id=BILLING, appointment_id=501) is None


def test_grant_for_a_different_patient_does_not_authorize_this_appointment():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=2001)  # not appointment 501's patient (1042)

    assert find_authorized_appointment(db, user_id=FRONTDESK, appointment_id=501) is None


def test_nonexistent_appointment_returns_none_indistinguishably_from_no_grant():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)

    # Same return value (None) as test_no_grant_at_all_returns_none — no way
    # for a caller to tell "no such appointment" from "not authorized".
    assert find_authorized_appointment(db, user_id=FRONTDESK, appointment_id=999999) is None


def test_revoked_grant_does_not_authorize():
    db = _fresh_session()
    _grant(
        db,
        user_id=FRONTDESK,
        patient_id=1042,
        revoked_at=datetime.datetime.now(datetime.timezone.utc),
    )

    assert find_authorized_appointment(db, user_id=FRONTDESK, appointment_id=501) is None


def test_expired_grant_does_not_authorize():
    db = _fresh_session()
    _grant(
        db,
        user_id=FRONTDESK,
        patient_id=1042,
        expires_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1),
    )

    assert find_authorized_appointment(db, user_id=FRONTDESK, appointment_id=501) is None


def test_not_yet_expired_grant_authorizes():
    db = _fresh_session()
    _grant(
        db,
        user_id=FRONTDESK,
        patient_id=1042,
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
    )

    assert find_authorized_appointment(db, user_id=FRONTDESK, appointment_id=501) is not None


def test_never_expiring_grant_authorizes():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042, expires_at=None)

    assert find_authorized_appointment(db, user_id=FRONTDESK, appointment_id=501) is not None


def test_disabled_users_grant_does_not_authorize():
    db = _fresh_session()
    _grant(db, user_id=DISABLED, patient_id=1042)

    assert find_authorized_appointment(db, user_id=DISABLED, appointment_id=501) is None


def test_one_users_grant_never_authorizes_another_user():
    db = _fresh_session()
    _grant(db, user_id=FRONTDESK, patient_id=1042)

    assert find_authorized_appointment(db, user_id=BILLING, appointment_id=501) is None


# --- latest_insurance_member_id ---------------------------------------------


def test_no_coverage_on_file_returns_none():
    db = _fresh_session()

    assert latest_insurance_member_id(db, patient_id=1042) is None


def test_returns_the_most_recently_created_coverages_member_id():
    db = _fresh_session()
    db.add(InsuranceCoverage(id=1, patient_id=1042, member_id="OLD-MEMBER-1"))
    db.add(InsuranceCoverage(id=2, patient_id=1042, member_id="NEW-MEMBER-2"))
    db.commit()

    assert latest_insurance_member_id(db, patient_id=1042) == "NEW-MEMBER-2"


def test_never_returns_another_patients_member_id():
    db = _fresh_session()
    db.add(InsuranceCoverage(id=1, patient_id=2001, member_id="OTHER-PATIENT-MEMBER"))
    db.commit()

    assert latest_insurance_member_id(db, patient_id=1042) is None


def test_a_coverage_row_with_no_member_id_is_skipped():
    db = _fresh_session()
    db.add(InsuranceCoverage(id=1, patient_id=1042, member_id=None))
    db.commit()

    assert latest_insurance_member_id(db, patient_id=1042) is None


# --- parse_user_id -----------------------------------------------------------


def test_parse_user_id_accepts_a_numeric_string():
    assert parse_user_id("2") == 2


def test_parse_user_id_rejects_none():
    assert parse_user_id(None) is None


def test_parse_user_id_rejects_empty_string():
    assert parse_user_id("") is None


def test_parse_user_id_rejects_non_numeric_text():
    assert parse_user_id("frontdesk") is None
