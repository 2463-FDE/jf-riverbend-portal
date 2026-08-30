"""W10 Final Stage 6 sub-slice 4 (batched in Stage 7 sub-slice 4, OBS-N01) —
services/records-service/app.py's RECORDS_LEGACY_N_PLUS_ONE_CHART_READS
counter increments exactly once per call to the chart-assembly path, GET
/patients/{patient_id}/records — and that path is now O(1) queries, not
O(encounters), after live smoke evidence proved the route is still called
by the current frontend.
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


def _grant_sql(db, user_id, patient_id):
    # Raw SQL, not the PatientAccessGrant ORM class: app.py never imports it
    # directly, and reaching it via sys.modules[<models module name>] is
    # fragile — multiple services each have their OWN same-named models.py,
    # all sharing the single 'models' key in sys.modules, so whichever
    # loaded last wins there regardless of which service's fixture asks.
    db.execute(app_mod.text(
        "INSERT INTO patient_access_grants (user_id, patient_id) VALUES (:user_id, :patient_id)"
    ), {"user_id": user_id, "patient_id": patient_id})


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
    _grant_sql(db, CLINICIAN_ID, PATIENT)
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


def test_an_audit_write_failure_returns_503_and_never_increments_the_counter(client, monkeypatch):
    """Review fix RECORDS-COUNTER-BEFORE-AUDIT: the counter represents a
    COMPLETED, auditable read — an audit-write failure must still surface
    its own 503 (unchanged) and must not have already counted the read as
    having happened."""
    def _raise(*a, **k):
        raise app_mod.HTTPException(status_code=503, detail="database unavailable")

    monkeypatch.setattr(app_mod, "_write_audit", _raise)
    before = RECORDS_LEGACY_N_PLUS_ONE_CHART_READS._value.get()

    resp = client.get(f"/patients/{PATIENT}/records", headers=_headers())

    assert resp.status_code == 503
    assert RECORDS_LEGACY_N_PLUS_ONE_CHART_READS._value.get() - before == 0


def test_the_batched_path_returns_the_correct_shape_and_ordering(client, monkeypatch):
    """OBS-N01: batching must not change the response — same
    PatientChart/EncounterWithRecords shape, encounters ordered by id,
    records ordered by id within each encounter."""
    db = app_mod.app.dependency_overrides[app_mod.get_db]()
    e1 = app_mod.Encounter(patient_id=PATIENT, provider="Dr. Patel", reason="Checkup")
    e2 = app_mod.Encounter(patient_id=PATIENT, provider="Dr. Patel", reason="Follow-up")
    db.add_all([e1, e2])
    db.flush()
    db.add_all([
        app_mod.Record(encounter_id=e2.id, patient_id=PATIENT, kind="NOTE", title="Second note", body="b"),
        app_mod.Record(encounter_id=e1.id, patient_id=PATIENT, kind="NOTE", title="First note", body="a"),
        app_mod.Record(encounter_id=e1.id, patient_id=PATIENT, kind="NOTE", title="First note 2", body="a2"),
    ])
    db.commit()

    resp = client.get(f"/patients/{PATIENT}/records", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["patient_id"] == PATIENT
    encounters = body["encounters"]
    assert [enc["encounter"]["id"] for enc in encounters] == sorted(enc["encounter"]["id"] for enc in encounters)
    first, second = encounters
    assert first["encounter"]["id"] == e1.id
    assert [r["title"] for r in first["records"]] == ["First note", "First note 2"]
    assert second["encounter"]["id"] == e2.id
    assert [r["title"] for r in second["records"]] == ["Second note"]


def test_the_batched_path_issues_a_bounded_number_of_queries_regardless_of_encounter_count(client, monkeypatch):
    """OBS-N01: the whole point of batching — query count must not grow
    with the number of encounters. Compares the zero-encounter case (this
    fixture's PATIENT, no encounters) against a patient with several
    encounters and multiple records each; the delta in db.execute() calls
    attributable to chart assembly must be exactly 1 (the batched records
    query), not one per encounter."""
    db = app_mod.app.dependency_overrides[app_mod.get_db]()
    many_patient_id = PATIENT + 1
    db.add(app_mod.Patient(id=many_patient_id, name="Many Encounters Patient"))
    db.flush()
    _grant_sql(db, CLINICIAN_ID, many_patient_id)
    for _ in range(5):
        enc = app_mod.Encounter(patient_id=many_patient_id, provider="Dr. Patel", reason="Visit")
        db.add(enc)
        db.flush()
        db.add_all([
            app_mod.Record(encounter_id=enc.id, patient_id=many_patient_id, kind="NOTE", title="t", body="b")
            for _ in range(3)
        ])
    db.commit()

    counts = {"n": 0}
    real_execute = db.execute

    def _counting_execute(*a, **k):
        counts["n"] += 1
        return real_execute(*a, **k)

    monkeypatch.setattr(db, "execute", _counting_execute)

    counts["n"] = 0
    resp_empty = client.get(f"/patients/{PATIENT}/records", headers=_headers())
    empty_case_calls = counts["n"]

    counts["n"] = 0
    resp_many = client.get(f"/patients/{many_patient_id}/records", headers=_headers())
    many_case_calls = counts["n"]

    assert resp_empty.status_code == 200
    assert resp_many.status_code == 200
    assert len(resp_many.json()["encounters"]) == 5
    assert many_case_calls - empty_case_calls == 1, (
        f"query count grew with encounter count ({empty_case_calls} -> {many_case_calls} "
        f"for 5 encounters) — batching regressed back to N+1"
    )
