"""Stage 3 — services/records-service/app.py::get_patient_view wiring.

Drives the real FastAPI route with a fake DB session (dependency override)
and a fake repository (monkeypatched in place of SqlChartRepository), so
this runs with no Postgres — mirroring tests/test_intake_endpoint.py's
direct-function/fake-session style. Confirms: the internal-token check
(review fix, round 2026-08-05) rejects a direct caller before authorization
ever runs, a real per-(actor, patient) grant lookup (Week 4 catch-up:
SqlPatientAccessGate, replacing the earlier authenticated-staff-only
StaffAccessGate) denies a missing actor (403) and allows a granted one (200)
once the token check passes, an invalid purpose is rejected before
authorization runs, a real audit_logs row is written on BOTH outcomes but
NEVER on an internal-token rejection, and (round-15 review, 2026-08-06) a
nonexistent patient_id is rejected with 404 before authorization or any
chart read runs, with no audit_logs row either.

FakeSession's `.execute()` stands in for SqlPatientAccessGate's grant
lookup — the ONLY place this route calls `db.execute()` (chart reads go
through FakeChartRepository below, never through this session), so a single
canned "was a grant row found" result is enough; the fake does not need to
parse the real SQL to know which actor/patient it's for.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_patient_view")

from libs.patient_view_agent.contracts import ChartResult, EncounterRow  # noqa: E402

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def _internal_header():
    return {"X-Internal-Token": TEST_TOKEN}


created_sessions = []



class _FakeActorRow:
    """The (role, is_active) row records-service now reads to enforce the
    signed permission matrix at the data boundary and to revalidate the actor
    on every request. `staff` is the legacy full-permission role every real
    account still carries, so it preserves what these tests were written to
    exercise: grant-based authorization, not role-based denial."""

    def __init__(self, role="staff", is_active=True):
        self.role = role
        self.is_active = is_active


class _FakeActorResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row

class _FakeGrantResult:
    def __init__(self, found: bool):
        self._found = found

    def first(self):
        return (1,) if self._found else None


class FakeSession:
    def __init__(
        self,
        *,
        fail_commit=False,
        existing_patient_ids=frozenset({1042}),
        grant_exists=True,
        fail_execute=False,
    ):
        self.added = []
        self.commit_count = 0
        self.rollback_count = 0
        self._fail_commit = fail_commit
        self._existing_patient_ids = existing_patient_ids
        self._grant_exists = grant_exists
        self._fail_execute = fail_execute

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        if self._fail_commit:
            raise SQLAlchemyError("simulated audit_logs write failure")
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def get(self, _model, pk):
        # Round-15 review: get_patient_view now checks patient existence via
        # db.get(Patient, patient_id) before authorizing/reading. Every
        # existing test in this file uses patient_id=1042 and expects it to
        # exist, hence the default.
        return object() if pk in self._existing_patient_ids else None

    def execute(self, _stmt):
        if self._fail_execute:
            raise SQLAlchemyError("simulated grant lookup failure")
        return _FakeGrantResult(self._grant_exists)


def _fake_get_db():
    session = FakeSession()
    created_sessions.append(session)
    yield session


def _fake_get_db_failing_commit():
    session = FakeSession(fail_commit=True)
    created_sessions.append(session)
    yield session


def _fake_get_db_missing_patient():
    session = FakeSession(existing_patient_ids=frozenset())
    created_sessions.append(session)
    yield session


def _fake_get_db_no_grant():
    session = FakeSession(grant_exists=False)
    created_sessions.append(session)
    yield session


def _fake_get_db_grant_lookup_failing():
    session = FakeSession(fail_execute=True)
    created_sessions.append(session)
    yield session


class FakeChartRepository:
    """Stands in for SqlChartRepository: one encounter, no records — enough
    for run_patient_view to reach a COMPLETED outcome with real evidence."""

    def __init__(self, db):
        self.db = db

    def load_chart(self, patient_id, *, correlation_id=""):
        return ChartResult(
            patient_id=patient_id,
            encounters=[
                EncounterRow(
                    id=1, patient_id=patient_id, encounter_type="office_visit", provider="Dr. Patel", status="finished"
                )
            ],
            records=[],
            reads=2,
        )


class FakeFailingChartRepository:
    """Round-19 review (2026-08-06): stands in for a real backend read
    failure — a schema-drifted column, a dropped connection, a flaky query
    — surfacing as SqlChartRepository.load_chart raising instead of
    returning a ChartResult. run_patient_view's own custom runtime catches
    this (libs/patient_view_agent/runtimes/custom.py) and converts it to
    outcome=ESCALATED, reasons=[NODE_FAILURE] — this fake exists to prove
    get_patient_view turns THAT into a 503, not a 200."""

    def __init__(self, db):
        self.db = db

    def load_chart(self, patient_id, *, correlation_id=""):
        raise SQLAlchemyError("simulated chart read failure")


@pytest.fixture
def client(monkeypatch):
    created_sessions.clear()
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


# --- internal-token check: the review fix (round, 2026-08-05) --------------


def test_direct_caller_with_spoofed_actor_and_no_token_is_rejected(client):
    # This is exactly the bypass the review flagged: a caller hitting this
    # service directly (as it would if reached via records-service's
    # published host port) with a made-up X-Actor-Id and no proof it came
    # through the gateway.
    resp = client.get("/patients/1042/view", headers={"X-Actor-Id": "attacker"})

    assert resp.status_code == 401
    # No audit row under the spoofed actor name — rejected before the grant
    # lookup (and therefore before any _write_audit call) ever runs.
    assert created_sessions[0].added == []


def test_wrong_token_with_valid_looking_actor_is_rejected(client):
    resp = client.get(
        "/patients/1042/view", headers={"X-Actor-Id": "1", "X-Actor-Name": "frontdesk", "X-Internal-Token": "not-the-real-token"}
    )
    assert resp.status_code == 401
    assert created_sessions[0].added == []


def test_unconfigured_token_fails_closed_even_with_matching_empty_values(client, monkeypatch):
    # The bug this guards against: if INTERNAL_SERVICE_TOKEN is unset on both
    # services, an empty configured value must NOT compare equal to an empty
    # header — that would silently reopen the exact bypass being fixed.
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "")
    resp = client.get("/patients/1042/view", headers={"X-Actor-Id": "1", "X-Actor-Name": "frontdesk", "X-Internal-Token": ""})
    assert resp.status_code == 401
    assert created_sessions[0].added == []


def test_short_placeholder_token_is_rejected_even_when_it_matches_exactly(client, monkeypatch):
    # Review round 2: .env.example used to ship INTERNAL_SERVICE_TOKEN=changeme
    # — a valid-looking placeholder a real deployment could ship unmodified.
    # A short, human-typed value must fail closed even if both sides somehow
    # agree on it (e.g. an operator typed "changeme" on both services).
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "changeme")
    resp = client.get("/patients/1042/view", headers={"X-Actor-Id": "1", "X-Actor-Name": "frontdesk", "X-Internal-Token": "changeme"})
    assert resp.status_code == 401
    assert created_sessions[0].added == []


# --- SqlPatientAccessGate, once the internal-token check passes -----------
# Week 4 catch-up: replaces StaffAccessGate (authenticated-staff-only) with
# a real per-(actor, patient) grant lookup.


def test_missing_actor_header_is_denied_and_audited(client):
    resp = client.get("/patients/1042/view", headers=_internal_header())

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "unknown_actor"

    assert len(created_sessions) == 1
    assert created_sessions[0].commit_count == 1
    audit = created_sessions[0].added[0]
    assert audit.actor == "unknown"
    assert "outcome=denied" in audit.message
    assert "patient_id=1042" in audit.message


def test_authorized_actor_gets_completed_view_and_is_audited(client):
    # `client`'s FakeSession defaults to grant_exists=True — standing in for
    # a real patient_access_grants row for this (actor, patient) pair.
    resp = client.get("/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "completed"
    assert body["patient_id"] == 1042
    assert body["evidence_ids"]

    audit = created_sessions[0].added[0]
    assert audit.actor == "frontdesk"
    assert "outcome=completed" in audit.message
    assert "patient_id=1042" in audit.message


def test_unauthorized_actor_is_denied_for_a_patient_they_have_no_grant_for(monkeypatch):
    # Week 4 catch-up: this replaces the old
    # test_a_different_actor_can_view_the_same_patient, which proved the
    # OPPOSITE — that StaffAccessGate allowed "an unrelated actor... for the
    # same patient_id" because it was authenticated-staff-only, not
    # patient-specific. SqlPatientAccessGate denies a real, known staff
    # account that simply has no grant row for this patient.
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_no_grant
    try:
        resp = TestClient(app_mod.app).get(
            "/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "2", "X-Actor-Name": "billing-clerk"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "not_authorized"
    audit = created_sessions[-1].added[0]
    assert audit.actor == "billing-clerk"
    assert "outcome=denied" in audit.message
    assert "patient_id=1042" in audit.message


def test_grant_lookup_failure_denies_closed(monkeypatch):
    # Database/policy failure during the grant lookup itself must deny, not
    # silently allow or 500 with chart data attached.
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_grant_lookup_failing
    try:
        resp = TestClient(app_mod.app).get(
            "/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "policy_error"
    assert "patient_id" not in resp.json()


def test_invalid_purpose_is_rejected_before_authorization(client):
    resp = client.get(
        "/patients/1042/view",
        params={"purpose": "not-a-real-purpose"},
        headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"},
    )
    assert resp.status_code == 400
    # get_db is a FastAPI dependency, so a session is still created, but
    # rejected before authorize() ever runs — no audit row for this request.
    assert created_sessions[0].added == []


def test_get_patient_records_now_requires_authorization():
    # Week 4 catch-up: get_patient_records was the actual RIV-201 IDOR
    # (DEBT D11) — this replaces the old test_legacy_records_endpoint_is_
    # unaffected, which asserted the OPPOSITE (that the endpoint still had
    # "no ownership / authorization check"). It now shares the exact same
    # SqlPatientAccessGate boundary as /view — proven end-to-end in
    # tests/test_records_authorization.py; this just guards the source-level
    # wiring against a silent revert.
    import inspect

    source = inspect.getsource(app_mod.get_patient_records)
    assert "_verify_internal_token" in source
    assert "_authorize_or_deny" in source
    assert "no ownership / authorization check" not in source


# --- audit-write-must-not-fail-open: the review fix (round 2, 2026-08-05) --


def test_allowed_view_does_not_return_chart_data_if_audit_write_fails(monkeypatch):
    # This is exactly what the review flagged: a prior version returned 200
    # with real chart data even though the access was never durably recorded.
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_failing_commit
    try:
        resp = TestClient(app_mod.app).get(
            "/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code != 200
    assert resp.status_code == 503
    assert "patient_id" not in resp.json()
    assert "evidence_ids" not in resp.json()


def test_denial_also_fails_closed_if_audit_write_fails(monkeypatch):
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_failing_commit
    try:
        resp = TestClient(app_mod.app).get("/patients/1042/view", headers=_internal_header())
    finally:
        app_mod.app.dependency_overrides.clear()

    # Even a denial must be durably recordable, or this now surfaces as a
    # 503 rather than silently confirming/denying with no trace.
    assert resp.status_code == 503


# --- patient existence check: round-15 review (2026-08-06) -----------------
#
# SqlChartRepository's first read only loads encounters for the requested
# id — an unknown id used to come back as an empty, evidence-free chart
# instead of a 404, so a typo'd or stale patient_id looked identical to a
# real patient with no records. get_patient_view now checks existence via
# db.get(Patient, patient_id) before authorization or any chart read.


def test_nonexistent_patient_id_returns_404_and_writes_no_audit_row(monkeypatch):
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_missing_patient
    try:
        resp = TestClient(app_mod.app).get(
            "/patients/999999/view", headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 404
    # Not-found, not a chart access — no audit_logs row, same reasoning as
    # the internal-token rejection path above.
    assert created_sessions[-1].added == []
    assert created_sessions[-1].commit_count == 0


def test_denied_actor_gets_403_not_404_for_a_nonexistent_patient(monkeypatch):
    # Round-19 review (2026-08-06): the opposite of what this test asserted
    # before. Authorization now runs before the existence check specifically
    # so a denied actor (missing X-Actor-Id) gets the SAME 403 whether
    # patient_id exists or not — the earlier "404 for a denied actor on a
    # missing id" behavior let a caller with the internal token but no valid
    # actor tell existing patient_ids apart from nonexistent ones (404 vs
    # 403), an existence oracle for exactly the callers this gate is
    # supposed to keep at zero reads.
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db_missing_patient
    try:
        resp = TestClient(app_mod.app).get("/patients/999999/view", headers=_internal_header())
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "unknown_actor"
    # Denied before db.get(Patient, ...) ever runs — the audit row records
    # the denial, same as test_missing_actor_header_is_denied_and_audited.
    audit = created_sessions[-1].added[0]
    assert audit.actor == "unknown"
    assert "outcome=denied" in audit.message


def test_denied_actor_gets_the_same_403_for_an_existing_patient_too(client):
    # The other half of the oracle check: an existing id (1042, the default
    # in FakeSession/the `client` fixture) must produce the identical
    # denial, not a different status that would let a denied caller
    # distinguish "exists" from "doesn't."
    resp = client.get("/patients/1042/view", headers=_internal_header())

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "unknown_actor"


# --- backend read failures must not look like a successful chart view -----
# round-19 review (2026-08-06)


def test_repository_failure_returns_503_not_200(monkeypatch):
    # Before this fix: SqlChartRepository raising was caught INSIDE
    # run_patient_view and turned into a normal outcome=ESCALATED result,
    # which this route returned as a plain 200 — a database error looked
    # identical to a real (if unhelpful) clinical answer to callers and
    # uptime monitors.
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeFailingChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db
    try:
        resp = TestClient(app_mod.app).get(
            "/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()

    assert resp.status_code == 503
    assert resp.status_code != 200


def test_repository_failure_still_writes_an_audit_row(monkeypatch):
    # The 503 above must not come at the cost of losing the access record —
    # this failure happens well after authorization, so it's still a real
    # (if unsuccessful) chart-view attempt.
    monkeypatch.setattr(app_mod, "SqlChartRepository", FakeFailingChartRepository)
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db
    try:
        TestClient(app_mod.app).get(
            "/patients/1042/view", headers={**_internal_header(), "X-Actor-Id": "1", "X-Actor-Name": "frontdesk"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()

    audit = created_sessions[-1].added[0]
    assert audit.actor == "frontdesk"
    assert "outcome=escalated" in audit.message
    assert "patient_id=1042" in audit.message
