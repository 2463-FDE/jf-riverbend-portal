"""RBAC enforcement tests — gateway route gating.

config/roles.yaml went from documentation nobody read to a live permission
source; these prove require_permission actually blocks a role from a route
the OLD flat `staff` role would have allowed, and that the role still
reaches its own permitted routes. Downstream calls are mocked (httpx) the
same way test_gateway_patients_route.py already does — a denial never even
reaches httpx, since require_permission runs before the route body.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_rbac")

VALID_TOKEN = "valid-token-abc"
TEST_INTERNAL_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def _session_for(role: str) -> dict:
    return {"user_id": "2", "username": "testuser", "role": role}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_INTERNAL_TOKEN)
    return TestClient(app_mod.app)


def _auth():
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _stub_downstream(monkeypatch, payload=None, status_code=200):
    monkeypatch.setattr(
        app_mod.httpx, "get", lambda *a, **k: _FakeResponse(status_code, payload)
    )
    monkeypatch.setattr(
        app_mod.httpx, "post", lambda *a, **k: _FakeResponse(status_code, payload)
    )


# --- roles_config itself, against the real config/roles.yaml file ---------


def test_front_desk_permissions_match_roles_yaml():
    from roles_config import permissions_for

    perms = permissions_for("front_desk")
    assert perms == {"patients.read", "patients.write", "records.read", "billing.read", "appointments.write"}


def test_clinician_permissions_match_roles_yaml():
    from roles_config import permissions_for

    perms = permissions_for("clinician")
    assert perms == {"patients.read", "records.read", "records.write"}


def test_roi_clerk_permissions_match_roles_yaml():
    from roles_config import permissions_for

    perms = permissions_for("roi_clerk")
    assert perms == {"patients.read", "disclosures.read", "roi.write"}


def test_scheduler_permissions_match_roles_yaml():
    from roles_config import permissions_for

    perms = permissions_for("scheduler")
    assert perms == {"patients.read", "appointments.write"}


def test_unknown_role_gets_no_permissions_fail_closed():
    from roles_config import permissions_for

    assert permissions_for("not-a-real-role") == set()


# --- clinician: cannot register a patient (front-desk-only permission) ----


def test_clinician_is_denied_intake(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("clinician") if t == VALID_TOKEN else None)

    resp = client.post("/intake", json={}, headers=_auth())

    assert resp.status_code == 403
    assert "patients.write" in resp.json()["detail"]


def test_front_desk_can_reach_intake(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("front_desk") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"patient_id": 1}, status_code=201)

    resp = client.post("/intake", json={}, headers=_auth())

    assert resp.status_code == 201


# --- front_desk: cannot write clinical/HL7 data (clinician-only permission) -


def test_front_desk_is_denied_hl7_ingest(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("front_desk") if t == VALID_TOKEN else None)

    resp = client.post("/hl7/ingest", json={}, headers=_auth())

    assert resp.status_code == 403
    assert "records.write" in resp.json()["detail"]


def test_clinician_can_reach_hl7_ingest(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("clinician") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"status": "ok"})

    resp = client.post("/hl7/ingest", json={}, headers=_auth())

    assert resp.status_code == 200


# --- roi_clerk: cannot read clinical chart records (clinician/front-desk only)


def test_roi_clerk_is_denied_chart_records(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("roi_clerk") if t == VALID_TOKEN else None)

    resp = client.get("/patients/1042/records", headers=_auth())

    assert resp.status_code == 403
    assert "records.read" in resp.json()["detail"]


def test_clinician_can_reach_chart_records(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("clinician") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"patient_id": 1042})

    resp = client.get("/patients/1042/records", headers=_auth())

    assert resp.status_code == 200


# --- scheduler: cannot see ROI/disclosure requests (roi_clerk-only) --------


def test_scheduler_is_denied_roi_requests(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("scheduler") if t == VALID_TOKEN else None)

    resp = client.get("/roi/requests", headers=_auth())

    assert resp.status_code == 403
    assert "disclosures.read" in resp.json()["detail"]


def test_roi_clerk_can_reach_roi_requests(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("roi_clerk") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"items": []})

    resp = client.get("/roi/requests", headers=_auth())

    assert resp.status_code == 200


def test_scheduler_can_book_an_appointment(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("scheduler") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"appointment_id": 1}, status_code=201)

    resp = client.post("/appointments", json={}, headers=_auth())

    assert resp.status_code == 201


def test_roi_clerk_is_denied_booking_an_appointment(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("roi_clerk") if t == VALID_TOKEN else None)

    resp = client.post("/appointments", json={}, headers=_auth())

    assert resp.status_code == 403
    assert "appointments.write" in resp.json()["detail"]


# --- the deprecated legacy `staff` role keeps every permission it had -----


def test_legacy_staff_role_is_unaffected_and_can_still_reach_everything(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("staff") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"ok": True}, status_code=200)

    for method, path, body in [
        ("post", "/intake", {}),
        ("post", "/hl7/ingest", {}),
        ("get", "/patients/1042/records", None),
        ("get", "/roi/requests", None),
        ("post", "/appointments", {}),
    ]:
        resp = client.request(method, path, json=body, headers=_auth())
        assert resp.status_code in (200, 201), f"{method.upper()} {path} unexpectedly denied for legacy staff role"


def test_anonymous_caller_is_still_rejected_before_any_permission_check(client):
    resp = client.get("/patients/1042/records")

    assert resp.status_code == 401
