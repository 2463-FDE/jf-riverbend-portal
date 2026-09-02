"""services/gateway/app.py::observability_status (GET /observability/status).

Demo-readiness slice: one safe place for a presenter to confirm the local
observability POC (Grafana/Prometheus/Loki/Tempo) is actually up, with
direct dashboard links, instead of Grafana's generic home page.

Gated on `patients.read` — the narrowest existing permission every
staff/clinician role holds and `patient` does not — never a public route,
and never a new role/permission added to config/roles.yaml. Every
dependency check must collapse any failure (timeout, connection refused,
non-200) to the categorical "unavailable", with no exception detail, host,
port, or config value ever reaching the response or the log.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from conftest import install_sqlite_users_db, load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_observability_status")

VALID_TOKEN = "valid-token-abc"
STAFF_SESSION = {"user_id": "2", "username": "frontdesk", "role": "front_desk", "security_version": "0"}
PATIENT_SESSION = {"user_id": "9", "username": "patient-1738", "role": "patient", "security_version": "0"}

_SESSIONS = {VALID_TOKEN: STAFF_SESSION, "patient-token": PATIENT_SESSION}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _SESSIONS.get(t))
    install_sqlite_users_db(app_mod, [
        app_mod.User(id=2, username="frontdesk", password_hash="x", role="front_desk", is_active=True),
        app_mod.User(id=9, username="patient-1738", password_hash="x", role="patient", is_active=True),
    ])
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def _auth(token=VALID_TOKEN):
    return {"Authorization": f"Bearer {token}"}


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def _all_ready_get(url, timeout=None):
    return _FakeResponse(200)


def test_anonymous_caller_is_rejected(client):
    resp = client.get("/observability/status")
    assert resp.status_code == 401


def test_patient_role_is_rejected_not_a_staff_session(client, monkeypatch):
    monkeypatch.setattr(app_mod.httpx, "get", _all_ready_get)

    resp = client.get("/observability/status", headers=_auth("patient-token"))

    assert resp.status_code == 403


def test_authenticated_staff_sees_ready_status_and_dashboard_links(client, monkeypatch):
    monkeypatch.setattr(app_mod.httpx, "get", _all_ready_get)
    monkeypatch.setattr(app_mod.settings, "grafana_public_url", "http://localhost:3000")

    resp = client.get("/observability/status", headers=_auth())

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["dependencies"] == {
        "grafana": "ready", "prometheus": "ready", "loki": "ready", "tempo": "ready",
    }
    assert body["dashboards"]["ai_agent_observability"] == (
        "http://localhost:3000/d/ai-agent-observability/ai-agent-observability"
    )
    assert body["dashboards"]["rag_evaluation"] == "http://localhost:3000/d/rag-evaluation/rag-evaluation"
    assert body["dashboards"]["riverbend_services"] == (
        "http://localhost:3000/d/riverbend-services/riverbend-services"
    )
    assert "rag-eval-publish" in body["note"]


def test_stack_not_started_is_degraded_not_an_error(client, monkeypatch):
    def _connection_refused(url, timeout=None):
        raise ConnectionError("[Errno 111] Connection refused to 10.0.0.7:3000 token=super-secret")

    monkeypatch.setattr(app_mod.httpx, "get", _connection_refused)

    resp = client.get("/observability/status", headers=_auth())

    assert resp.status_code == 200  # never a 500 just because observability isn't up
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["dependencies"] == {
        "grafana": "unavailable", "prometheus": "unavailable", "loki": "unavailable", "tempo": "unavailable",
    }


def test_partial_outage_is_degraded_with_per_dependency_detail(client, monkeypatch):
    def _grafana_down_rest_up(url, timeout=None):
        if "grafana" in url:
            raise TimeoutError("timed out")
        return _FakeResponse(200)

    monkeypatch.setattr(app_mod.httpx, "get", _grafana_down_rest_up)

    resp = client.get("/observability/status", headers=_auth())

    body = resp.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["grafana"] == "unavailable"
    assert body["dependencies"]["prometheus"] == "ready"
    assert body["dependencies"]["loki"] == "ready"
    assert body["dependencies"]["tempo"] == "ready"


def test_no_sensitive_detail_in_response_or_logs(client, monkeypatch, caplog):
    secret = "super-secret-internal-token-do-not-leak"

    def _leaky_failure(url, timeout=None):
        raise RuntimeError(f"connection to {url} failed, internal_service_token={secret}")

    monkeypatch.setattr(app_mod.httpx, "get", _leaky_failure)

    with caplog.at_level(logging.INFO):
        resp = client.get("/observability/status", headers=_auth())

    raw_response = resp.text
    assert secret not in raw_response
    assert "grafana:3000" not in raw_response  # no compose-internal host, only the public dashboard base
    assert "RuntimeError" not in raw_response
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "grafana:3000" not in record.getMessage()
        assert "RuntimeError" not in record.getMessage()
