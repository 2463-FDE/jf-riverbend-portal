"""Stage 2 (Week 6) — services/records-service/app.py::get_patient_reconciliation.

Drives the real FastAPI route with a fake DB session (dependency override,
no real Postgres) whose `execute()` distinguishes a `select(Patient)` scan
from a `select(Encounter).where(Encounter.patient_id == ...)` lookup by
inspecting the compiled statement — mirrors tests/test_records_patient_view_route.py's
harness style, extended because this route issues its own raw queries
(reconciliation.py) rather than going through a repository class.

Fixture mirrors the real seeded Maria Gonzalez case (db/seed/patients.csv/
encounters.csv, patients 1042/1330/1588) rather than inventing new PHI-shaped
data, per CLAUDE.md.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_reconciliation")

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def _internal_header():
    return {"X-Internal-Token": TEST_TOKEN}


class _FakeQueryResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakePatient:
    def __init__(self, id, name, dob, ssn):
        self.id = id
        self.name = name
        self.dob = dob
        self.ssn = ssn


class _FakeEncounter:
    def __init__(self, id, patient_id, allergies=None, medications=None):
        self.id = id
        self.patient_id = patient_id
        self.allergies = allergies
        self.medications = medications


# The real seeded fixture (db/seed/patients.csv, db/seed/encounters.csv):
# three "Maria Gonzalez" rows sharing one SSN, one differing (transposed) dob,
# and a penicillin allergy recorded only under 1330.
_MARIA_1042 = _FakePatient(1042, "Maria Gonzalez", "1971-03-02", "412-55-9981")
_MARIA_1330 = _FakePatient(1330, "Maria Gonzales", "1971-03-02", "412-55-9981")
_MARIA_1588 = _FakePatient(1588, "M. Gonzalez", "1971-02-03", "412-55-9981")
_UNRELATED = _FakePatient(2001, "James O'Brien", "1980-05-01", "999-00-1111")

_ENCOUNTERS = [
    _FakeEncounter(1, 1042, allergies=None, medications="lisinopril"),
    _FakeEncounter(2, 1330, allergies="penicillin", medications="amoxicillin"),
    _FakeEncounter(3, 1588, allergies=None, medications=None),
    _FakeEncounter(4, 2001, allergies=None, medications=None),
    # Round-1 fix: the real seed generator sometimes writes a "no known
    # allergy" phrase literally into this free-text column instead of
    # leaving it blank — must not be treated as a candidate allergen value.
    _FakeEncounter(5, 1042, allergies="none known", medications=None),
]

created_sessions = []


class FakeSession:
    def __init__(self, *, patients, encounters=_ENCOUNTERS, fail_commit=False, patient_lookup_ids=None):
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0
        self._fail_commit = fail_commit
        self.patients = patients
        self.encounters = encounters
        # ids db.get(Patient, ...) will resolve — defaults to every fake patient given
        self._lookup_ids = patient_lookup_ids if patient_lookup_ids is not None else {p.id for p in patients}

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        if self._fail_commit:
            raise SQLAlchemyError("simulated audit_logs write failure")
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def get(self, _model, pk):
        if pk not in self._lookup_ids:
            return None
        return next((p for p in self.patients if p.id == pk), None)

    def execute(self, stmt, _params=None):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is app_mod.Patient:
            return _FakeQueryResult(self.patients)
        if entity is app_mod.Encounter:
            patient_id = stmt.whereclause.right.value
            return _FakeQueryResult([e for e in self.encounters if e.patient_id == patient_id])
        raise AssertionError(f"unexpected query entity: {entity}")


def _fake_get_db_factory(**session_kwargs):
    def _fake_get_db():
        session = FakeSession(**session_kwargs)
        created_sessions.append(session)
        yield session

    return _fake_get_db


def _request(client_or_app, patient_id, *, headers):
    return client_or_app.get(f"/patients/{patient_id}/reconciliation", headers=headers)


def _with_session(session_kwargs, call):
    created_sessions.clear()
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_factory(**session_kwargs)
    app_mod.settings.internal_service_token = TEST_TOKEN
    try:
        return call(TestClient(app_mod.app))
    finally:
        app_mod.app.dependency_overrides.clear()


# --- internal-token check (same fail-closed contract as get_patient_view) --


def test_missing_internal_token_is_rejected():
    resp = _with_session(
        {"patients": [_MARIA_1042]},
        lambda c: c.get("/patients/1042/reconciliation", headers={"X-Actor-Id": "frontdesk"}),
    )
    assert resp.status_code == 401
    assert created_sessions[0].added == []


# --- StaffAccessGate ---------------------------------------------------------


def test_missing_actor_is_denied_and_audited_as_reconciliation():
    resp = _with_session(
        {"patients": [_MARIA_1042]},
        lambda c: c.get("/patients/1042/reconciliation", headers=_internal_header()),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "unknown_actor"

    audit = created_sessions[0].added[0]
    assert audit.actor == "unknown"
    # Round-6 fix: must say "reconciliation", not the shared helper's default
    # "patient_view" label, so the audit trail names the route that was denied.
    assert "reconciliation outcome=denied" in audit.message
    assert "patient_id=1042" in audit.message


def test_denied_actor_gets_403_not_404_for_a_nonexistent_patient():
    resp = _with_session(
        {"patients": [], "patient_lookup_ids": set()},
        lambda c: c.get("/patients/999999/reconciliation", headers=_internal_header()),
    )
    assert resp.status_code == 403
    assert created_sessions[0].added[0].actor == "unknown"


# --- patient existence, after authorization ---------------------------------


def test_nonexistent_patient_id_returns_404_and_writes_no_audit_row():
    resp = _with_session(
        {"patients": [], "patient_lookup_ids": set()},
        lambda c: c.get(
            "/patients/999999/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 404
    assert created_sessions[0].added == []
    assert created_sessions[0].commit_count == 0


# --- no match found -----------------------------------------------------------


def test_patient_with_no_ssn_match_returns_only_itself_and_no_escalation():
    resp = _with_session(
        {"patients": [_UNRELATED]},
        lambda c: c.get(
            "/patients/2001/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["escalation"] is False
    assert len(body["source_records"]) == 1
    assert body["source_records"][0]["is_requested_patient"] is True
    assert body["identity_signals"] == []
    assert body["discrepancies"] == []

    audit = created_sessions[0].added[0]
    assert "reconciliation outcome=completed" in audit.message
    assert "match_count=0" in audit.message


# --- the Maria Gonzalez fixture: exact-SSN match + allergy discrepancy ------


def test_exact_ssn_matches_are_returned_with_masked_identity_signal():
    resp = _with_session(
        {"patients": [_MARIA_1042, _MARIA_1330, _MARIA_1588]},
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["escalation"] is True

    ids = {r["patient_id"] for r in body["source_records"]}
    assert ids == {1042, 1330, 1588}

    requested = next(r for r in body["source_records"] if r["patient_id"] == 1042)
    assert requested["is_requested_patient"] is True
    assert requested["source_label"] == "Current chart"
    match = next(r for r in body["source_records"] if r["patient_id"] == 1330)
    assert match["source_label"] == "Possible match"

    # Never the raw ssn anywhere in the response.
    assert "412-55-9981" not in resp.text
    assert body["identity_signals"] == [{"signal_type": "ssn_exact_match", "masked_value": "•••-••-9981"}]


def test_penicillin_allergy_discrepancy_is_flagged_with_evidence():
    resp = _with_session(
        {"patients": [_MARIA_1042, _MARIA_1330, _MARIA_1588]},
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    body = resp.json()

    allergy_discrepancies = [d for d in body["discrepancies"] if d["category"] == "allergy"]
    assert len(allergy_discrepancies) == 1
    disc = allergy_discrepancies[0]
    assert disc["value"] == "penicillin"
    assert disc["present_on_patient_ids"] == [1330]
    assert set(disc["missing_on_patient_ids"]) == {1042, 1588}
    assert disc["review_required"] is True
    assert "PATIENT:1330" in disc["evidence_ids"]
    assert "ENCOUNTER:2" in disc["evidence_ids"]

    audit = created_sessions[0].added[0]
    assert "match_count=2" in audit.message
    assert "discrepancy_count=" in audit.message


def test_no_known_allergy_phrase_is_not_treated_as_a_candidate_value():
    resp = _with_session(
        {"patients": [_MARIA_1042, _MARIA_1330, _MARIA_1588]},
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    body = resp.json()
    requested = next(r for r in body["source_records"] if r["patient_id"] == 1042)
    assert "none known" not in requested["allergies"]
    assert requested["allergies"] == []
    assert all(d["value"] != "none known" for d in body["discrepancies"])


def test_limitations_flag_free_text_and_unconfirmed_identity():
    resp = _with_session(
        {"patients": [_MARIA_1042, _MARIA_1330, _MARIA_1588]},
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    limitations = " ".join(resp.json()["limitations"]).lower()
    assert "not confirmed proof" in limitations
    assert "free-text" in limitations


# --- audit-write-must-not-fail-open ------------------------------------------


def test_does_not_return_data_if_audit_write_fails():
    resp = _with_session(
        {"patients": [_MARIA_1042, _MARIA_1330], "fail_commit": True},
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 503
    assert "source_records" not in resp.json()
