"""Stage 2 (Week 6) — services/records-service/app.py::get_patient_reconciliation.

Drives the real FastAPI route with a fake DB session (dependency override,
no real Postgres) whose `execute()` distinguishes a `select(Patient)` scan,
a `select(Encounter).where(Encounter.patient_id == ...)` lookup, and a
`select(PatientAccessGrant...)` grant check by inspecting the compiled
statement's entity and bound parameters — mirrors
tests/test_records_patient_view_route.py's harness style, extended because
this route issues its own raw queries (reconciliation.py) rather than going
through a repository class.

Fixture mirrors the real seeded Maria Gonzalez case (db/seed/patients.csv/
encounters.csv, patients 1042/1330/1588) rather than inventing new PHI-shaped
data, per CLAUDE.md.

Week 4 catch-up (Codex review, 2026-08-07, PR #22 — high, no-ship): this
route now runs real per-(actor, patient) authorization
(SqlPatientAccessGate) on the requested patient AND independently on every
SSN-matched candidate (authorized_patient_ids), replacing the earlier
authenticated-staff-only StaffAccessGate this file's tests used to exercise.
The grant-aware sections below (marked "Week 4 catch-up") are the actual
regression coverage for the cross-patient disclosure fix; everything above
them is the pre-existing matching/discrepancy/audit coverage, updated only
where the new authorization boundary changes what a test needs to set up.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_reconciliation")

from models import PatientAccessGrant  # noqa: E402

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

    def first(self):
        return self._items[0] if self._items else None


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
# A valid-but-unique SSN (area 245 isn't a reserved/never-issued range) —
# not 999-... like the old fixture used, since that area code is now
# rejected outright by _normalize_ssn's SSA-invalid-range check and would
# test "invalid SSN" instead of the intended "genuinely no match" case.
_UNRELATED = _FakePatient(2001, "James O'Brien", "1980-05-01", "245-67-8901")

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
    def __init__(
        self,
        *,
        patients,
        encounters=_ENCOUNTERS,
        fail_commit=False,
        patient_lookup_ids=None,
        granted=None,
        fail_grant_lookup=False,
        fail_batch_grant_lookup=False,
    ):
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0
        self._fail_commit = fail_commit
        self.patients = patients
        self.encounters = encounters
        # ids db.get(Patient, ...) will resolve — defaults to every fake patient given
        self._lookup_ids = patient_lookup_ids if patient_lookup_ids is not None else {p.id for p in patients}
        # actor username -> set of patient_ids that actor holds an active
        # grant for. Defaults to "frontdesk is granted every patient given"
        # — every pre-authorization-boundary test in this file used
        # "frontdesk" and expected it to see everything, so this preserves
        # that behavior for tests that aren't specifically about exclusion.
        self._granted = granted if granted is not None else {"frontdesk": {p.id for p in patients}}
        self._fail_grant_lookup = fail_grant_lookup
        self._fail_batch_grant_lookup = fail_batch_grant_lookup

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
            # Round 4 review: find_ssn_match_ids does a lean, unfiltered
            # id+ssn scan (no bound params — every patient given, matching
            # find_ssn_matches' old behavior); _fetch_patients_by_id does a
            # SEPARATE select(Patient).where(Patient.id.in_(authorized_ids))
            # for full detail on an already-known, already-authorized id
            # set only — that one compiles a bound `id_1` list. Distinguish
            # by bound params, same technique as the PatientAccessGrant
            # branch below, so an authorized-subset detail fetch doesn't
            # accidentally return every patient regardless of the filter.
            id_filter = stmt.compile().params.get("id_1")
            if id_filter is not None:
                wanted = set(id_filter) if isinstance(id_filter, (list, set, tuple)) else {id_filter}
                return _FakeQueryResult([p for p in self.patients if p.id in wanted])
            return _FakeQueryResult(self.patients)
        if entity is app_mod.Encounter:
            patient_id = stmt.whereclause.right.value
            return _FakeQueryResult([e for e in self.encounters if e.patient_id == patient_id])
        if entity is PatientAccessGrant:
            # Bound-parameter extraction (not WHERE-clause structure
            # inspection) so this doesn't care whether the caller is
            # SqlPatientAccessGate's single .first() lookup (the requested
            # patient) or authorized_patient_ids' batch .scalars().all() one
            # (SSN-matched candidates) — both compile to a `username_1` +
            # `patient_id_1` (scalar or list) bind regardless of the extra
            # revoked_at/expires_at clauses; `isinstance(..., list)` is what
            # tells the two apart, which lets fail_batch_grant_lookup fail
            # only the candidate check without also failing the initial
            # requested-patient authorization that runs before it.
            params = stmt.compile().params
            username = params.get("username_1", "")
            requested_ids = params.get("patient_id_1")
            is_batch = isinstance(requested_ids, (list, set, tuple))
            if self._fail_grant_lookup or (is_batch and self._fail_batch_grant_lookup):
                raise SQLAlchemyError("simulated grant lookup failure")
            allowed = self._granted.get(username, set())
            if is_batch:
                matched_ids = [pid for pid in requested_ids if pid in allowed]
            else:
                matched_ids = [requested_ids] if requested_ids in allowed else []
            return _FakeQueryResult(matched_ids)
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


# --- SqlPatientAccessGate on the requested patient --------------------------


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
    # "patient_access" label, so the audit trail names the route that was denied.
    assert "reconciliation outcome=denied" in audit.message
    assert "patient_id=1042" in audit.message


def test_denied_actor_gets_403_not_404_for_a_nonexistent_patient():
    resp = _with_session(
        {"patients": [], "patient_lookup_ids": set()},
        lambda c: c.get("/patients/999999/reconciliation", headers=_internal_header()),
    )
    assert resp.status_code == 403
    assert created_sessions[0].added[0].actor == "unknown"


def test_authorized_actor_can_view_the_requested_patient():
    # Week 4 catch-up: an actor who IS granted the requested patient still
    # gets a normal 200 — the new boundary doesn't regress the allowed path.
    resp = _with_session(
        {"patients": [_UNRELATED], "granted": {"frontdesk": {2001}}},
        lambda c: c.get(
            "/patients/2001/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["source_records"][0]["patient_id"] == 2001


def test_actor_without_a_grant_for_the_requested_patient_is_denied():
    # Week 4 catch-up: unlike the old StaffAccessGate, being a real known
    # actor is not enough — a grant for THIS patient is required.
    resp = _with_session(
        {"patients": [_UNRELATED], "granted": {"frontdesk": set()}},
        lambda c: c.get(
            "/patients/2001/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "not_authorized"


def test_grant_lookup_failure_denies_closed():
    resp = _with_session(
        {"patients": [_UNRELATED], "fail_grant_lookup": True},
        lambda c: c.get(
            "/patients/2001/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "policy_error"


def test_candidate_grant_lookup_failure_returns_503_not_a_clean_no_match_result():
    # Codex review (2026-08-07, PR #22 — medium): authorized_patient_ids used
    # to swallow a DB failure into an empty set, so this exact scenario —
    # candidate discovery succeeds (1330 genuinely shares 1042's SSN), but
    # the batch grant lookup on those candidates fails — used to come back
    # as a normal 200 with escalation=False, indistinguishable from a
    # genuine no-match case. It must now be a 503, not a silent "no matches."
    resp = _with_session(
        {
            "patients": [_MARIA_1042, _MARIA_1330],
            "granted": {"frontdesk": {1042, 1330}},
            "fail_batch_grant_lookup": True,
        },
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 503
    assert "source_records" not in resp.json()


# --- patient existence, after authorization ---------------------------------


def test_nonexistent_patient_id_returns_404_and_writes_no_audit_row():
    # Explicitly granted despite not existing in `patients` — isolates "is
    # authorized but doesn't exist" (404) from "isn't authorized" (403),
    # which the default `granted` (derived from the empty `patients` list)
    # would otherwise conflate.
    resp = _with_session(
        {"patients": [], "patient_lookup_ids": set(), "granted": {"frontdesk": {999999}}},
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
# These tests use the default `granted` (frontdesk authorized for every
# patient in the fixture) — they're about matching/discrepancy logic, not
# the authorization boundary, which gets its own section below.


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


# --- Week 4 catch-up: per-candidate authorization on SSN matches -----------
# This is the actual regression coverage for the cross-patient disclosure a
# review flagged as high/no-ship: a prior version returned every SSN-matched
# candidate's details on the strength of the requested patient's own
# authorization alone. All three Maria Gonzalez rows (1042/1330/1588) share
# one SSN, so this fixture is exactly the shape that finding described.


def test_authorized_matched_candidate_appears_with_full_detail():
    resp = _with_session(
        {
            "patients": [_MARIA_1042, _MARIA_1330],
            "granted": {"frontdesk": {1042, 1330}},
        },
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = {r["patient_id"] for r in body["source_records"]}
    assert ids == {1042, 1330}
    match = next(r for r in body["source_records"] if r["patient_id"] == 1330)
    assert match["name_on_file"] == "Maria Gonzales"


def test_unauthorized_matched_candidate_is_completely_absent():
    # frontdesk is granted the requested patient (1042) but NOT the
    # SSN-matched candidate (1330), even though 1330 genuinely shares the
    # SSN. 1330 must not appear anywhere in the response.
    resp = _with_session(
        {
            "patients": [_MARIA_1042, _MARIA_1330],
            "granted": {"frontdesk": {1042}},
        },
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = {r["patient_id"] for r in body["source_records"]}
    assert ids == {1042}
    assert 1330 not in ids

    # No candidate-enumeration signal: the response looks exactly like "no
    # match found at all," not "a match exists but you can't see it."
    assert body["escalation"] is False
    assert body["identity_signals"] == []
    assert body["discrepancies"] == []

    # Nothing about the excluded patient anywhere in the raw response body.
    assert "1330" not in resp.text
    assert "Gonzales" not in resp.text  # 1330's exact name_on_file value


def test_mixed_authorized_and_unauthorized_candidates_returns_only_authorized():
    # Three-way SSN match; frontdesk is granted 1042 (requested) and 1330,
    # but NOT 1588. 1588 must be excluded while 1330 still appears normally.
    resp = _with_session(
        {
            "patients": [_MARIA_1042, _MARIA_1330, _MARIA_1588],
            "granted": {"frontdesk": {1042, 1330}},
        },
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = {r["patient_id"] for r in body["source_records"]}
    assert ids == {1042, 1330}
    assert 1588 not in ids
    assert "1588" not in resp.text
    assert "M. Gonzalez" not in resp.text  # 1588's exact name_on_file value

    # match_count in the audit trail reflects only the AUTHORIZED match —
    # the excluded candidate is not counted either.
    audit = created_sessions[0].added[0]
    assert "match_count=1" in audit.message


def test_unauthorized_candidate_contributes_no_discrepancy_evidence():
    # 1330 (penicillin allergy) is the one that would normally produce an
    # allergy discrepancy against 1042/1588. If 1330 is unauthorized, that
    # discrepancy — and its evidence ids — must not appear either.
    resp = _with_session(
        {
            "patients": [_MARIA_1042, _MARIA_1330, _MARIA_1588],
            "granted": {"frontdesk": {1042, 1588}},
        },
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert {r["patient_id"] for r in body["source_records"]} == {1042, 1588}
    assert all(d["category"] != "allergy" or d["value"] != "penicillin" for d in body["discrepancies"])
    assert "penicillin" not in resp.text
    assert "PATIENT:1330" not in resp.text
    assert "ENCOUNTER:2" not in resp.text


def test_no_authorized_candidates_looks_identical_to_no_match_at_all():
    # frontdesk is granted ONLY the requested patient — none of the real
    # SSN matches. The response must be indistinguishable from a genuine
    # no-match case (test_patient_with_no_ssn_match_returns_only_itself_
    # and_no_escalation above), not a "0 visible of N found" signal.
    resp = _with_session(
        {
            "patients": [_MARIA_1042, _MARIA_1330, _MARIA_1588],
            "granted": {"frontdesk": {1042}},
        },
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "frontdesk"}
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "patient_id": 1042,
        "identity_signals": [],
        "source_records": [
            {
                "patient_id": 1042,
                "is_requested_patient": True,
                "source_label": "Current chart",
                "name_on_file": "Maria Gonzalez",
                "dob": "1971-03-02",
                "allergies": [],
                "medications": ["lisinopril"],
            }
        ],
        "discrepancies": [],
        "limitations": body["limitations"],  # static list, not asserted here
        "escalation": False,
        "correlation_id": body["correlation_id"],
    }


def test_a_different_actor_with_broader_grants_sees_the_full_match_set():
    # The flip side of the exclusion tests above: a DIFFERENT actor who
    # genuinely holds grants for all three charts sees all three — proving
    # the fix is real per-actor scoping, not an accidental global lockdown.
    resp = _with_session(
        {
            "patients": [_MARIA_1042, _MARIA_1330, _MARIA_1588],
            "granted": {"drnguyen": {1042, 1330, 1588}},
        },
        lambda c: c.get(
            "/patients/1042/reconciliation", headers={**_internal_header(), "X-Actor-Id": "drnguyen"}
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert {r["patient_id"] for r in body["source_records"]} == {1042, 1330, 1588}
    assert body["escalation"] is True


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
