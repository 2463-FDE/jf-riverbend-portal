"""Coverage & Eligibility workspace (W9.3) — gateway routes.

Every route here is scoped by patient_id AND coverage_id, on top of the role
permission require_permission already checks — this file is about the layer
require_permission does NOT cover: an active patient_access_grants row, and
the coverage actually belonging to the named patient. Downstream calls to
eligibility-service are mocked (httpx), the same way test_gateway_rbac.py
already does for other proxied routes.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_coverage")

TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
VALID = "valid-token-abc"
PATIENT = 1737
OTHER_PATIENT = 1042
BILLING_USER_ID = 950
READONLY_USER_ID = 951
UNGRANTED_BILLING_USER_ID = 952


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    app_mod.User.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def fake_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app_mod.app.dependency_overrides[app_mod.get_db] = fake_db
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)

    with Session() as s:
        s.add_all([
            app_mod.Patient(id=PATIENT, name="Priya Khan"),
            app_mod.Patient(id=OTHER_PATIENT, name="Maria Gonzalez"),
            app_mod.User(id=BILLING_USER_ID, username="billing1", password_hash="x", role="billing", is_active=True),
            app_mod.User(id=READONLY_USER_ID, username="mgr1", password_hash="x", role="management", is_active=True),
            app_mod.User(id=UNGRANTED_BILLING_USER_ID, username="billing2", password_hash="x", role="billing", is_active=True),
        ])
        s.add(app_mod.InsuranceCoverage(
            id=1, patient_id=PATIENT, payer_name="Acme Health", member_id="ABC123456789",
            group_number="GRP-1", plan_type="PPO", status="unknown",
        ))
        s.add(app_mod.InsuranceCoverage(id=2, patient_id=OTHER_PATIENT, payer_name="Acme Health",
                                         member_id="ZZZ999", plan_type="HMO", status="unknown"))
        s.add(app_mod.PatientAccessGrant(user_id=BILLING_USER_ID, patient_id=PATIENT))
        s.add(app_mod.PatientAccessGrant(user_id=READONLY_USER_ID, patient_id=PATIENT))
        # UNGRANTED_BILLING_USER_ID deliberately holds no grant for PATIENT.
        s.commit()

    yield TestClient(app_mod.app), Session
    app_mod.app.dependency_overrides.clear()


def _session_for(monkeypatch, user_id, role):
    monkeypatch.setattr(
        app_mod, "get_session",
        lambda t: {"user_id": str(user_id), "username": "x", "role": role, "security_version": "0"}
        if t == VALID else None,
    )


def _auth():
    return {"Authorization": f"Bearer {VALID}"}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def test_billing_read_can_list_coverages_with_masked_member_id(env, monkeypatch):
    client, _ = env
    _session_for(monkeypatch, BILLING_USER_ID, "billing")

    resp = client.get(f"/patients/{PATIENT}/coverages", headers=_auth())

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["member_id_masked"] == "********6789"
    assert "member_id" not in item, "the full member id must never leave this route"
    assert item["payer_name"] == "Acme Health"


def test_management_holds_billing_read_but_not_write(env, monkeypatch):
    client, _ = env
    _session_for(monkeypatch, READONLY_USER_ID, "management")

    listing = client.get(f"/patients/{PATIENT}/coverages", headers=_auth())
    assert listing.status_code == 200

    verify = client.post(f"/patients/{PATIENT}/coverages/1/verify", headers=_auth())
    assert verify.status_code == 403, "management has no billing.write per config/roles.yaml"


def test_cross_patient_coverage_id_is_refused_as_not_found(env, monkeypatch):
    """Coverage 2 belongs to OTHER_PATIENT, not PATIENT — asking for it
    through PATIENT's own path must 404, not leak whose it actually is."""
    client, _ = env
    _session_for(monkeypatch, BILLING_USER_ID, "billing")

    resp = client.get(f"/patients/{PATIENT}/coverages/2/eligibility-status", headers=_auth())
    assert resp.status_code == 404


def test_an_ungranted_billing_user_is_denied_even_with_the_right_role(env, monkeypatch):
    client, _ = env
    _session_for(monkeypatch, UNGRANTED_BILLING_USER_ID, "billing")

    assert client.get(f"/patients/{PATIENT}/coverages", headers=_auth()).status_code == 403
    assert client.post(f"/patients/{PATIENT}/coverages/1/verify", headers=_auth()).status_code == 403


def test_simulated_mode_makes_no_outbound_call_and_never_claims_active(env, monkeypatch):
    client, _ = env
    _session_for(monkeypatch, BILLING_USER_ID, "billing")
    monkeypatch.setattr(app_mod.settings, "payer_api_key", "")  # blank — the training-env default

    called = []
    monkeypatch.setattr(app_mod.httpx, "post", lambda *a, **k: called.append(1) or _FakeResponse())
    monkeypatch.setattr(app_mod.httpx, "get", lambda *a, **k: called.append(1) or _FakeResponse())

    resp = client.post(f"/patients/{PATIENT}/coverages/1/verify", headers=_auth())

    assert resp.status_code == 201
    assert resp.json()["category"] == "simulated"
    assert "no payer contacted" in resp.json()["message"].lower()
    assert called == [], "simulation must place no outbound call at all"


def test_a_real_verification_creates_a_job_and_status_maps_the_result(env, monkeypatch):
    client, Session = env
    _session_for(monkeypatch, BILLING_USER_ID, "billing")
    monkeypatch.setattr(app_mod.settings, "payer_api_key", "test-key-not-actually-real")
    monkeypatch.setattr(app_mod.settings, "payer_integration_mode", "live")

    monkeypatch.setattr(
        app_mod.httpx, "post",
        lambda url, **k: _FakeResponse(201, {
            "job_id": "job-abc", "status": "queued", "manual_retry_count": 0, "max_manual_retries": 1,
        }),
    )
    verify = client.post(f"/patients/{PATIENT}/coverages/1/verify", headers=_auth())
    assert verify.status_code == 201
    assert verify.json()["category"] == "pending"
    assert "job_id" not in verify.json(), "a job id must never reach the browser"

    with Session() as s:
        stored = s.get(app_mod.InsuranceCoverage, 1)
        assert stored.verification_job_id == "job-abc"

    monkeypatch.setattr(
        app_mod.httpx, "get",
        lambda url, **k: _FakeResponse(200, {
            "job_id": "job-abc", "status": "succeeded", "result_status": "active",
            "manual_retry_count": 0, "max_manual_retries": 1,
        }),
    )
    status = client.get(f"/patients/{PATIENT}/coverages/1/eligibility-status", headers=_auth())
    assert status.status_code == 200
    assert status.json()["category"] == "active"

    with Session() as s:
        stored = s.get(app_mod.InsuranceCoverage, 1)
        assert stored.status == "active"
        assert stored.verified_at is not None


def test_a_second_verify_while_one_is_in_flight_reuses_it(env, monkeypatch):
    """Idempotent: a repeated Verify click while the first check is still
    queued/running must not start a second live payer call."""
    client, Session = env
    _session_for(monkeypatch, BILLING_USER_ID, "billing")
    monkeypatch.setattr(app_mod.settings, "payer_api_key", "test-key-not-actually-real")
    monkeypatch.setattr(app_mod.settings, "payer_integration_mode", "live")

    with Session() as s:
        coverage = s.get(app_mod.InsuranceCoverage, 1)
        coverage.verification_job_id = "job-inflight"
        s.commit()

    post_calls = []
    monkeypatch.setattr(app_mod.httpx, "post", lambda url, **k: post_calls.append(url) or _FakeResponse(201, {"job_id": "job-NEW"}))
    monkeypatch.setattr(
        app_mod.httpx, "get",
        lambda url, **k: _FakeResponse(200, {"job_id": "job-inflight", "status": "running",
                                              "manual_retry_count": 0, "max_manual_retries": 1}),
    )

    resp = client.post(f"/patients/{PATIENT}/coverages/1/verify", headers=_auth())

    assert resp.status_code == 201
    assert resp.json()["category"] == "pending"
    assert post_calls == [], "must reuse the in-flight job, not create a second one"


def test_dead_letter_reports_unavailable_and_a_bounded_retry(env, monkeypatch):
    client, Session = env
    _session_for(monkeypatch, BILLING_USER_ID, "billing")

    with Session() as s:
        coverage = s.get(app_mod.InsuranceCoverage, 1)
        coverage.verification_job_id = "job-dead"
        s.commit()

    monkeypatch.setattr(
        app_mod.httpx, "get",
        lambda url, **k: _FakeResponse(200, {"job_id": "job-dead", "status": "dead_letter",
                                              "manual_retry_count": 1, "max_manual_retries": 3}),
    )
    status = client.get(f"/patients/{PATIENT}/coverages/1/eligibility-status", headers=_auth())
    assert status.json() == {"category": "unavailable", "can_retry": True}

    monkeypatch.setattr(
        app_mod.httpx, "post",
        lambda url, **k: _FakeResponse(200, {"job_id": "job-dead", "status": "retryable",
                                              "manual_retry_count": 2, "max_manual_retries": 3}),
    )
    retried = client.post(f"/patients/{PATIENT}/coverages/1/eligibility-retry", headers=_auth())
    assert retried.status_code == 200
    assert retried.json()["category"] == "unavailable"
    assert retried.json()["can_retry"] is True
