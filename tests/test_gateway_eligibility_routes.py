"""Tests for the gateway's Stage 3 proxy routes: eligibility job status/
retry and visit-scoped assistant turns (services/gateway/app.py).

Same auth posture as every other gateway route — Depends(require_session) —
so these tests check both "no unauthenticated exposure was added" and that
upstream status codes (404/409/503) are faithfully forwarded rather than
flattened to a blanket 200, which the frontend polling surface depends on.
"""
import json

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
    # w-9-2-planner P1a review fix (B1-row-mismatch): insurance_id is derived
    # from the SAME row as coverage_on_file (coverage.member_id), never from
    # a separately-mocked lookup — so a caller wanting insurance_id set but
    # no other coverage fields gets a bare _FakeCoverage(member_id=...).
    if coverage is None and insurance_id is not None:
        coverage = _FakeCoverage(member_id=insurance_id)
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
    # w-9-2-planner P1a review fix (B1-row-mismatch): insurance_id is now
    # always derived from the SAME row as coverage_on_file, so a fixture
    # that gives an insurance_id necessarily produces a matching (masked)
    # coverage_on_file rather than None.
    assert captured["json"] == {
        "message": "am I covered?",
        "patient_id": 1042,
        "insurance_id": "BCBS-9981",
        "coverage_on_file": {
            "payer_name": None,
            "plan_type": None,
            "member_id_masked": "*****9981",
            "status": None,
            "verified_at": None,
        },
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

    monkeypatch.setattr(app_mod, "latest_insurance_coverage", raise_db_error)

    resp = client.post("/visits/1/messages", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 503


def test_visit_message_derives_insurance_id_from_the_same_row_as_coverage_on_file(client, monkeypatch):
    # w-9-2-planner P1a review fix (B1-row-mismatch): insurance_id must come
    # from the SAME coverage row as coverage_on_file, not a second,
    # independently-selected row. A coverage row with no member_id must
    # yield insurance_id=None even if an older row on file does have one —
    # verification should decline rather than verify a stale identity.
    _authorize(
        monkeypatch,
        appointment=_FakeAppointment(patient_id=1042),
        coverage=_FakeCoverage(payer_name="Aetna", plan_type="PPO", member_id=None, status="active"),
    )
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(
            200,
            {"visit_id": "1", "reply": "ok", "tool_called": False, "termination_reason": "answered", "turns_used": 1},
        )

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/visits/1/messages", json={"message": "am I covered?"}, headers=_auth())

    assert resp.status_code == 200
    assert captured["json"]["insurance_id"] is None
    assert captured["json"]["coverage_on_file"]["payer_name"] == "Aetna"


def test_visit_message_downstream_unreachable_is_a_502(client, monkeypatch):
    _authorize(monkeypatch, appointment=_FakeAppointment(patient_id=1042), insurance_id=None)

    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.httpx, "post", fake_post)

    resp = client.post("/visits/1/messages", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 502


# --- visit-chat streaming route (w-9-2-planner P1b) --------------------------
#
# Authorization must happen entirely BEFORE the downstream stream ever
# opens — these tests assert that an unauthorized/invalid request never
# opens an httpx.Client at all, the same "denial before any call" property
# already proven for the blocking route and for appointments elsewhere in
# this suite.
#
# w-9-2-planner P1b review fix (STREAM-UPSTREAM-STATUS): the route now opens
# the upstream connection with httpx.Client(...).send(request, stream=True)
# instead of the httpx.stream(...) context manager, specifically so the
# upstream status code can be inspected and forwarded BEFORE any
# StreamingResponse (which commits to 200) is ever constructed. The fake
# below stands in for both the Client and the response it returns, since
# send() on it simply returns itself.


class _FakeStreamingClient:
    def __init__(self, *, status_code=200, chunks=None, json_body=None, send_error=None):
        self.status_code = status_code
        self._chunks = chunks or []
        self._json_body = json_body
        self._send_error = send_error
        self.closed = False
        self.built_request = None

    def build_request(self, method, url, json=None, headers=None):
        self.built_request = {"method": method, "url": url, "json": json, "headers": headers}
        return self.built_request

    def send(self, request, stream=False):
        if self._send_error is not None:
            raise self._send_error
        return self

    def iter_bytes(self):
        yield from self._chunks

    def json(self):
        if self._json_body is None:
            raise ValueError("no body")
        return self._json_body

    @property
    def text(self):
        return json.dumps(self._json_body) if self._json_body is not None else ""

    def close(self):
        self.closed = True


def test_visit_message_stream_non_numeric_visit_id_is_rejected_before_any_stream_call(client, monkeypatch):
    called = []
    monkeypatch.setattr(app_mod.httpx, "Client", lambda **k: called.append(1) or _FakeStreamingClient())

    resp = client.post("/visits/not-a-number/messages/stream", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 403
    assert called == []


def test_visit_message_stream_unauthorized_appointment_is_rejected_before_any_stream_call(client, monkeypatch):
    _authorize(monkeypatch, appointment=None)
    called = []
    monkeypatch.setattr(app_mod.httpx, "Client", lambda **k: called.append(1) or _FakeStreamingClient())

    resp = client.post("/visits/999/messages/stream", json={"message": "am I covered?"}, headers=_auth())

    assert resp.status_code == 403
    assert called == []


def test_visit_message_stream_grant_lookup_failure_is_503_before_any_stream_call(client, monkeypatch):
    called = []

    def raise_db_error(db, **kw):
        raise app_mod.SQLAlchemyError("simulated grant lookup failure")

    monkeypatch.setattr(app_mod, "find_authorized_appointment", raise_db_error)
    monkeypatch.setattr(app_mod.httpx, "Client", lambda **k: called.append(1) or _FakeStreamingClient())

    resp = client.post("/visits/1/messages/stream", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 503
    assert called == []


def test_visit_message_stream_relays_the_authorized_streams_bytes_using_server_derived_fields(client, monkeypatch):
    _authorize(monkeypatch, appointment=_FakeAppointment(patient_id=1042), insurance_id="BCBS-9981")
    fake_client = _FakeStreamingClient(
        status_code=200, chunks=[b'{"kind": "delta", "text": "You"}\n', b'{"kind": "done"}\n']
    )
    monkeypatch.setattr(app_mod.httpx, "Client", lambda **k: fake_client)

    resp = client.post(
        "/visits/1/messages/stream",
        json={"message": "am I covered?", "patient_id": 9999, "insurance_id": "SMUGGLED"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.text == '{"kind": "delta", "text": "You"}\n{"kind": "done"}\n'
    assert fake_client.built_request["method"] == "POST"
    assert fake_client.built_request["url"].endswith("/visits/1/messages/stream")
    # Ignores caller-supplied patient_id/insurance_id — server-derived only,
    # same guarantee as the blocking route.
    assert fake_client.built_request["json"]["patient_id"] == 1042
    assert fake_client.built_request["json"]["insurance_id"] == "BCBS-9981"
    assert fake_client.closed is True  # connection released once fully relayed


def test_visit_message_stream_forwards_a_non_2xx_upstream_status_instead_of_flattening_to_200(client, monkeypatch):
    # The exact defect STREAM-UPSTREAM-STATUS closes: eligibility-service
    # replying 401/422/503 must not be hidden behind an always-200 stream.
    _authorize(monkeypatch, appointment=_FakeAppointment(patient_id=1042), insurance_id=None)
    fake_client = _FakeStreamingClient(status_code=503, json_body={"detail": "eligibility-service unavailable"})
    monkeypatch.setattr(app_mod.httpx, "Client", lambda **k: fake_client)

    resp = client.post("/visits/1/messages/stream", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 503
    assert resp.json() == {"detail": "eligibility-service unavailable"}
    assert fake_client.closed is True  # never left dangling once the status is known bad


def test_visit_message_stream_connection_open_failure_is_a_real_502_not_a_flattened_200(client, monkeypatch):
    # Before any upstream status is even known (DNS/connection-refused), the
    # failure must be a real 502 — not disguised as a successful 200 stream
    # the way a naive try/except around the whole relay used to produce.
    # Mirrors _post's own forward_status=True 502 shape exactly (same
    # {"error": str(e)} body) — not a new leakage surface introduced here.
    _authorize(monkeypatch, appointment=_FakeAppointment(patient_id=1042), insurance_id=None)
    fake_client = _FakeStreamingClient(send_error=httpx.ConnectError("connection refused"))
    monkeypatch.setattr(app_mod.httpx, "Client", lambda **k: fake_client)

    resp = client.post("/visits/1/messages/stream", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 502
    assert fake_client.closed is True


def test_visit_message_stream_mid_stream_failure_after_a_committed_200_ends_in_one_sanitized_error_line(
    client, monkeypatch
):
    # Once the 200 status is already committed (a real upstream connection
    # opened successfully), a failure DURING iteration has no HTTP status
    # left to change — it must still end the stream with one sanitized
    # terminal event, not a silently truncated response read as complete.
    _authorize(monkeypatch, appointment=_FakeAppointment(patient_id=1042), insurance_id=None)
    secret_detail = "connection refused to 10.0.0.7:1234 — do not leak this"

    class _MidStreamFailure(_FakeStreamingClient):
        def iter_bytes(self):
            yield b'{"kind": "delta", "text": "Part"}\n'
            raise httpx.ReadError(secret_detail)

    fake_client = _MidStreamFailure(status_code=200)
    monkeypatch.setattr(app_mod.httpx, "Client", lambda **k: fake_client)

    resp = client.post("/visits/1/messages/stream", json={"message": "hi"}, headers=_auth())

    assert resp.status_code == 200
    assert secret_detail not in resp.text
    lines = [json.loads(line) for line in resp.text.strip().splitlines()]
    assert lines[0] == {"kind": "delta", "text": "Part"}
    assert lines[-1]["kind"] == "error"
    assert lines[-1]["termination_reason"] == "provider_error"
    assert fake_client.closed is True
