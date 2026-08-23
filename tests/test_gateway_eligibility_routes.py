"""Tests for the gateway's Stage 3 proxy routes: eligibility job status/
retry and visit-scoped assistant turns (services/gateway/app.py).

Same auth posture as every other gateway route — Depends(require_session) —
so these tests check both "no unauthenticated exposure was added" and that
upstream status codes (404/409/503) are faithfully forwarded rather than
flattened to a blanket 200, which the frontend polling surface depends on.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app")

VALID_TOKEN = "valid-token-abc"
_VALID_SESSION = {"user_id": "2", "username": "frontdesk", "role": "staff"}


@pytest.fixture
def client(monkeypatch):
    def fake_get_session(token):
        return _VALID_SESSION if token == VALID_TOKEN else None

    monkeypatch.setattr(app_mod, "get_session", fake_get_session)
    return TestClient(app_mod.app)


def _auth():
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


# --- auth gating: no new unauthenticated exposure -----------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/eligibility/jobs/job-1"),
        ("post", "/eligibility/jobs/job-1/retry"),
        ("post", "/visits/1/messages"),
    ],
)
def test_new_routes_reject_anonymous_callers(client, method, path):
    kwargs = {"json": {"message": "hi"}} if method == "post" and "messages" in path else {}
    resp = getattr(client, method)(path, **kwargs)

    assert resp.status_code == 401


# --- job status: forwards upstream status + body ------------------------------


def test_job_status_ok_is_proxied_through(client, monkeypatch):
    body = {"job_id": "job-1", "status": "queued", "retry_count": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "X-Request-Id" in headers
        assert url.endswith("/eligibility/jobs/job-1")
        return _FakeResponse(200, body)

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/eligibility/jobs/job-1", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == body


def test_job_status_404_is_forwarded_not_flattened_to_200(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(404, {"detail": "job not found"})

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/eligibility/jobs/does-not-exist", headers=_auth())

    assert resp.status_code == 404


def test_job_status_downstream_unreachable_is_a_502_not_a_bare_200(client, monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "get", fake_get)

    resp = client.get("/eligibility/jobs/job-1", headers=_auth())

    assert resp.status_code == 502


# --- retry: forwards 409 conflict ---------------------------------------------


def test_retry_conflict_is_forwarded_as_409(client, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/eligibility/jobs/job-1/retry")
        return _FakeResponse(409, {"job_id": "job-1", "status": "queued"})

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/eligibility/jobs/job-1/retry", headers=_auth())

    assert resp.status_code == 409


def test_retry_success_is_forwarded_as_200(client, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(200, {"job_id": "job-1", "status": "retryable"})

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/eligibility/jobs/job-1/retry", headers=_auth())

    assert resp.status_code == 200
    assert resp.json()["status"] == "retryable"


# --- visit-chat: Stage 2 (feature-readiness) authorization gate ---------------
#
# visit_id is now required to be a real appointments.id, gated on the
# session's user_id holding an active patient_access_grants row for that
# appointment's patient (visit_authorization.py). These tests mock
# find_authorized_appointment/latest_insurance_member_id directly rather than
# hitting a real Postgres — that module has its own dedicated unit tests
# (tests/test_gateway_visit_authorization.py) against real query behavior.


class _FakeAppointment:
    def __init__(self, patient_id):
        self.patient_id = patient_id


class _FakeCoverage:
    def __init__(self, *, payer_name=None, plan_type=None, member_id=None, status=None, verified_at=None):
        self.payer_name = payer_name
        self.plan_type = plan_type
        self.member_id = member_id
        self.status = status
        self.verified_at = verified_at


def _authorize(monkeypatch, *, appointment=None, insurance_id=None, coverage=None):
    monkeypatch.setattr(app_mod, "find_authorized_appointment", lambda db, **kw: appointment)
    monkeypatch.setattr(app_mod, "latest_insurance_member_id", lambda db, **kw: insurance_id)
    # w-9-2-planner P1a: proxy_visit_message now also derives a stored
    # coverage-on-file snapshot — defaults to None (no coverage on file) so
    # every existing test here that doesn't care about it keeps working
    # without touching a real DB session.
    monkeypatch.setattr(app_mod, "latest_insurance_coverage", lambda db, **kw: coverage)


def test_visit_message_non_numeric_visit_id_is_rejected_without_a_grant_lookup(client, monkeypatch):
    calls = {"n": 0}

    def fail_if_called(db, **kw):
        calls["n"] += 1
        raise AssertionError("must not look up a grant for a non-numeric visit_id")

    monkeypatch.setattr(app_mod, "find_authorized_appointment", fail_if_called)

    resp = client.post("/visits/not-a-number/messages", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 403
    assert calls["n"] == 0


def test_visit_message_unauthorized_appointment_is_rejected_with_403(client, monkeypatch):
    _authorize(monkeypatch, appointment=None)  # no grant / no such appointment — same response either way

    resp = client.post("/visits/999/messages", json={"message": "am I covered?"}, headers=_auth())

    assert resp.status_code == 403


def test_visit_message_authorized_appointment_forwards_server_derived_fields(client, monkeypatch):
    _authorize(monkeypatch, appointment=_FakeAppointment(patient_id=1042), insurance_id="BCBS-9981")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(
            200,
            {"visit_id": "1", "reply": "ok", "tool_called": False, "termination_reason": "answered", "turns_used": 1},
        )

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/visits/1/messages", json={"message": "am I covered?"}, headers=_auth())

    assert resp.status_code == 200
    assert captured["url"].endswith("/visits/1/messages")
    assert captured["json"] == {
        "message": "am I covered?",
        "patient_id": 1042,
        "insurance_id": "BCBS-9981",
        "coverage_on_file": None,  # no coverage row in this test's fixture
    }
    assert "X-Request-Id" in captured["headers"]
    # Opaque, uuid4-hex shaped — not derived from the session/username.
    correlation_id = captured["headers"]["X-Request-Id"]
    assert len(correlation_id) == 32
    assert "frontdesk" not in correlation_id


def test_visit_message_forwards_a_masked_coverage_on_file_snapshot(client, monkeypatch):
    # w-9-2-planner P1a: coverage_on_file is server-derived the same way
    # patient_id/insurance_id already are, and never carries the full member
    # id — only the same masked form the Coverage & Eligibility page shows.
    _authorize(
        monkeypatch,
        appointment=_FakeAppointment(patient_id=1737),
        insurance_id="KAISER5591",
        coverage=_FakeCoverage(
            payer_name="Kaiser",
            plan_type="HMO",
            member_id="KAISER5591",
            status="active",
            verified_at=None,
        ),
    )
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(
            200,
            {"visit_id": "1", "reply": "ok", "tool_called": False, "termination_reason": "answered", "turns_used": 1},
        )

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/visits/1/messages", json={"message": "what's on file?"}, headers=_auth())

    assert resp.status_code == 200
    coverage_on_file = captured["json"]["coverage_on_file"]
    assert coverage_on_file["payer_name"] == "Kaiser"
    assert coverage_on_file["plan_type"] == "HMO"
    assert coverage_on_file["status"] == "active"
    assert coverage_on_file["member_id_masked"] == "******5591"
    # The full member id legitimately appears in top-level insurance_id —
    # verify_current_eligibility needs it for the live payer call. Only
    # coverage_on_file's own copy must be masked.
    assert "KAISER5591" not in str(coverage_on_file)


def test_visit_message_ignores_client_supplied_patient_and_insurance_id(client, monkeypatch):
    # The exact defect this stage exists to close: a caller-supplied
    # patient_id/insurance_id must never reach eligibility-service, even for
    # an appointment the caller IS authorized for.
    _authorize(monkeypatch, appointment=_FakeAppointment(patient_id=1042), insurance_id="BCBS-9981")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(
            200,
            {"visit_id": "1", "reply": "ok", "tool_called": False, "termination_reason": "answered", "turns_used": 1},
        )

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post(
        "/visits/1/messages",
        json={"message": "am I covered?", "patient_id": 9999, "insurance_id": "SMUGGLED-ID"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert captured["json"]["patient_id"] == 1042
    assert captured["json"]["insurance_id"] == "BCBS-9981"


def test_visit_message_grant_lookup_failure_returns_503(client, monkeypatch):
    def raise_db_error(db, **kw):
        raise app_mod.SQLAlchemyError("simulated grant lookup failure")

    monkeypatch.setattr(app_mod, "find_authorized_appointment", raise_db_error)

    resp = client.post("/visits/1/messages", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 503


def test_visit_message_insurance_lookup_failure_returns_503(client, monkeypatch):
    monkeypatch.setattr(app_mod, "find_authorized_appointment", lambda db, **kw: _FakeAppointment(patient_id=1042))

    def raise_db_error(db, **kw):
        raise app_mod.SQLAlchemyError("simulated insurance lookup failure")

    monkeypatch.setattr(app_mod, "latest_insurance_member_id", raise_db_error)

    resp = client.post("/visits/1/messages", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 503


def test_visit_message_downstream_unreachable_is_a_502(client, monkeypatch):
    _authorize(monkeypatch, appointment=_FakeAppointment(patient_id=1042), insurance_id=None)

    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/visits/1/messages", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 502
