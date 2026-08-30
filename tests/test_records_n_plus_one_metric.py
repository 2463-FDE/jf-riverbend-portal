"""W10 Final Stage 6 sub-slice 4 — services/records-service/app.py's
RECORDS_LEGACY_N_PLUS_ONE_CHART_READS counter increments exactly once per
call to the deliberate N+1 chart-assembly path (DEBT D8), GET
/patients/{patient_id}/records.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module
from libs.metrics.business import RECORDS_LEGACY_N_PLUS_ONE_CHART_READS

app_mod = load_module("services/records-service/app.py", "records_n_plus_one_metric_app")

TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
PATIENT = 1737
CLINICIAN_ID = 900


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    app_mod.AgentDraftProvenance.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add_all([
        app_mod.Patient(id=PATIENT, name="Demo Patient"),
        app_mod.User(id=CLINICIAN_ID, username="drkim", role="clinician", is_active=True),
    ])
    db.flush()
    # Raw SQL, not the PatientAccessGrant ORM class: app.py never imports it
    # directly, and reaching it via sys.modules[<models module name>] is
    # fragile — multiple services each have their OWN same-named models.py,
    # all sharing the single 'models' key in sys.modules, so whichever
    # loaded last wins there regardless of which service's fixture asks.
    db.execute(app_mod.text(
        "INSERT INTO patient_access_grants (user_id, patient_id) VALUES (:user_id, :patient_id)"
    ), {"user_id": CLINICIAN_ID, "patient_id": PATIENT})
    db.commit()

    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = lambda: db
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()
    db.close()


def _headers():
    return {"X-Internal-Token": TOKEN, "X-Actor-Id": str(CLINICIAN_ID), "X-Actor-Name": "drkim"}


def test_a_successful_chart_read_increments_the_n_plus_one_counter_exactly_once(client):
    before = RECORDS_LEGACY_N_PLUS_ONE_CHART_READS._value.get()

    resp = client.get(f"/patients/{PATIENT}/records", headers=_headers())

    assert resp.status_code == 200
    assert RECORDS_LEGACY_N_PLUS_ONE_CHART_READS._value.get() - before == 1


def test_a_denied_read_never_increments_the_counter(client):
    before = RECORDS_LEGACY_N_PLUS_ONE_CHART_READS._value.get()

    # No grant exists for this patient — must be denied before any query.
    resp = client.get("/patients/999999/records", headers=_headers())

    assert resp.status_code == 403
    assert RECORDS_LEGACY_N_PLUS_ONE_CHART_READS._value.get() - before == 0
