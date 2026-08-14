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


# --- roles_config against the real config/roles.yaml -----------------------
#
# These assert the client's signed permission matrix (2026-08-13) cell for
# cell. If the matrix is amended, these change with it — they are the
# executable copy of the contract, not incidental coverage.


def test_front_desk_permissions_match_the_signed_matrix():
    from roles_config import permissions_for

    assert permissions_for("front_desk") == {
        "patients.read", "patients.write",
        "billing.read", "billing.write",
        "appointments.read", "appointments.write",
        "consents.read", "consents.write",
    }


def test_clinician_permissions_match_the_signed_matrix():
    from roles_config import permissions_for

    assert permissions_for("clinician") == {
        "patients.read", "records.read", "records.write",
        "appointments.read", "consents.read",
    }


def test_nursing_ma_matches_clinician_until_the_note_split_is_funded():
    # Deliberately identical: the client declined splitting nursing
    # documentation from physician notes this cycle. Pinned so the day someone
    # DOES split it, this test is what tells them the roles diverge.
    from roles_config import permissions_for

    assert permissions_for("nursing_ma") == permissions_for("clinician")


def test_lab_holds_write_without_read():
    # The unusual one, and the client's own revision: with no separate results
    # category, "read prior results" would mean reading the whole chart. Write
    # must not imply read anywhere downstream.
    from roles_config import permissions_for

    perms = permissions_for("lab")
    assert perms == {"patients.read", "records.write"}
    assert "records.read" not in perms


def test_billing_permissions_match_the_signed_matrix():
    from roles_config import permissions_for

    assert permissions_for("billing") == {
        "patients.read", "billing.read", "billing.write",
        "appointments.read", "consents.read",
    }


def test_roi_clerk_permissions_match_the_signed_matrix():
    from roles_config import permissions_for

    perms = permissions_for("roi_clerk")
    assert perms == {
        "patients.read", "consents.read", "disclosures.read",
        "roi.write", "audit.read",
    }
    # Amended 2026-08-13, reversing the earlier answer: clerks work from the
    # document list, never the note body.
    assert "records.read" not in perms


def test_scheduler_permissions_match_the_signed_matrix():
    from roles_config import permissions_for

    assert permissions_for("scheduler") == {
        "patients.read", "appointments.read", "appointments.write",
    }


def test_it_admin_has_no_patient_data_at_all():
    # The client's explicit instruction: manages accounts, no chart read. Not
    # demographics either — nothing patient-scoped.
    from roles_config import permissions_for

    perms = permissions_for("it_admin")
    assert perms == {"accounts.read", "accounts.write", "audit.read"}
    assert not any(p.startswith(("patients.", "records.", "consents.")) for p in perms)


def test_management_has_no_demographics_or_notes():
    from roles_config import permissions_for

    perms = permissions_for("management")
    assert perms == {
        "billing.read", "appointments.read", "disclosures.read",
        "accounts.read", "audit.read",
    }
    assert "patients.read" not in perms
    assert "records.read" not in perms


def test_no_operational_role_can_read_clinical_notes_except_the_clinical_ones():
    # The single most important property of the whole grid.
    from roles_config import permissions_for

    may_read_notes = {
        r for r in ("front_desk", "clinician", "nursing_ma", "lab", "billing",
                    "roi_clerk", "scheduler", "it_admin", "management")
        if "records.read" in permissions_for(r)
    }
    assert may_read_notes == {"clinician", "nursing_ma"}


def test_legacy_staff_keeps_full_patient_data_access_but_no_admin_or_audit():
    # It must not lose access as the vocabulary grows — every existing account
    # is still on it — and must not silently gain account admin or the audit
    # log, which the legacy role never had.
    from roles_config import permissions_for

    perms = permissions_for("staff")
    for p in ("patients.write", "records.read", "records.write", "billing.write",
              "appointments.read", "consents.write", "disclosures.read", "roi.write"):
        assert p in perms, f"legacy staff lost {p}"
    assert "accounts.read" not in perms
    assert "accounts.write" not in perms
    assert "audit.read" not in perms


def test_unknown_role_gets_no_permissions_fail_closed():
    from roles_config import permissions_for

    assert permissions_for("not-a-real-role") == set()
    assert permissions_for("") == set()
    assert permissions_for(None) == set()


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


# --- the 13 Aug amendments, at the route level -----------------------------


def test_front_desk_is_now_denied_the_chart(client, monkeypatch):
    # The headline behavioural change: front_desk previously held records.read
    # and could pull any authorized chart. The client ruled that out.
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("front_desk") if t == VALID_TOKEN else None)

    resp = client.get("/patients/1042/records", headers=_auth())

    assert resp.status_code == 403
    assert "records.read" in resp.json()["detail"]


def test_front_desk_is_also_denied_the_reconciliation_view(client, monkeypatch):
    # Consequence worth being explicit about rather than discovering later:
    # /reconciliation is gated on records.read and surfaces allergies and
    # medications, so removing front_desk's note access removes the
    # duplicate-review view built for registration staff in Week 6. Raised to
    # the client in the PR — the grid says no clinical data for front desk, and
    # this view carries clinical data.
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("front_desk") if t == VALID_TOKEN else None)

    resp = client.get("/patients/1042/reconciliation", headers=_auth())

    assert resp.status_code == 403


def test_front_desk_keeps_demographics_and_scheduling(client, monkeypatch):
    # The other half: losing the chart must not cost front desk the routes it
    # needs to actually register and schedule.
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("front_desk") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"ok": True})

    assert client.get("/patients/1042", headers=_auth()).status_code == 200
    assert client.get("/patients", headers=_auth()).status_code == 200
    assert client.post("/appointments", json={}, headers=_auth()).status_code == 200


def test_roi_clerk_is_denied_the_chart_after_the_amendment(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("roi_clerk") if t == VALID_TOKEN else None)

    assert client.get("/patients/1042/records", headers=_auth()).status_code == 403
    assert client.get("/records/search", params={"q": "x"}, headers=_auth()).status_code == 403


def test_lab_can_write_results_but_cannot_read_the_chart(client, monkeypatch):
    # Write-without-read, end to end: /hl7/ingest needs records.write, the
    # chart routes need records.read. Lab has exactly one of those.
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("lab") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"ok": True})

    assert client.post("/hl7/ingest", json={}, headers=_auth()).status_code == 200
    assert client.get("/patients/1042/records", headers=_auth()).status_code == 403


def test_it_admin_cannot_reach_any_patient_route(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("it_admin") if t == VALID_TOKEN else None)

    for path in ("/patients", "/patients/1042", "/patients/1042/records", "/records/search"):
        params = {"q": "x"} if path == "/records/search" else None
        assert client.get(path, params=params, headers=_auth()).status_code == 403, path


def test_management_reads_reporting_surfaces_but_no_patient_data(client, monkeypatch):
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("management") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"items": []})

    assert client.get("/roi/requests", headers=_auth()).status_code == 200
    assert client.get("/patients/1042", headers=_auth()).status_code == 403
    assert client.get("/patients/1042/records", headers=_auth()).status_code == 403


# --- /slots (PR #26 gated it; these are the tests that PR lost) -------------
#
# The gating landed on main but its tests did not survive the branch shuffle.
# Re-added here because this branch owns the current shape of this file.
#
# appointments.write rather than appointments.read is deliberate and correct
# under the signed matrix: you look at open slots in order to book one, and the
# roles that book are exactly front_desk, scheduler and legacy staff. Clinician,
# nursing, billing and management hold appointments.read to see a patient's
# booked appointments — not to shop for availability.


def test_slots_requires_a_booking_permission(client, monkeypatch):
    # Previously reachable by any authenticated session, which left the
    # authorization model inconsistent across the scheduling endpoints.
    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("roi_clerk") if t == VALID_TOKEN else None)

    resp = client.get("/slots", headers=_auth())

    assert resp.status_code == 403
    assert "appointments.write" in resp.json()["detail"]


def test_slots_is_reachable_by_exactly_the_roles_that_book(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"items": []})

    for role in ("front_desk", "scheduler", "staff"):
        monkeypatch.setattr(app_mod, "get_session", lambda t, r=role: _session_for(r) if t == VALID_TOKEN else None)
        assert client.get("/slots", headers=_auth()).status_code == 200, f"{role} should reach /slots"

    for role in ("clinician", "nursing_ma", "lab", "billing", "roi_clerk", "it_admin", "management"):
        monkeypatch.setattr(app_mod, "get_session", lambda t, r=role: _session_for(r) if t == VALID_TOKEN else None)
        assert client.get("/slots", headers=_auth()).status_code == 403, f"{role} should not reach /slots"


def test_slots_still_rejects_an_anonymous_caller(client):
    assert client.get("/slots").status_code == 401


# --- /appointments (PR #31 review [high]) -----------------------------------
#
# It was gated on patients.read, but the signed matrix makes the two distinct:
# roi_clerk and lab hold patients.read and NOT appointments.read, so they could
# list any patient's appointment history by supplying a patient_id. The grid
# says appointments are None for both.


def test_reading_appointments_requires_the_appointments_permission(client, monkeypatch):
    for role in ("roi_clerk", "lab", "it_admin"):
        monkeypatch.setattr(app_mod, "get_session", lambda t, r=role: _session_for(r) if t == VALID_TOKEN else None)
        resp = client.get("/appointments", params={"patient_id": 1042}, headers=_auth())
        assert resp.status_code == 403, f"{role} should not read appointments"
        assert "appointments.read" in resp.json()["detail"]


def test_roles_that_need_appointments_can_still_read_them(client, monkeypatch):
    _stub_downstream(monkeypatch, payload={"items": []})

    for role in ("front_desk", "scheduler", "clinician", "billing", "management", "staff"):
        monkeypatch.setattr(app_mod, "get_session", lambda t, r=role: _session_for(r) if t == VALID_TOKEN else None)
        resp = client.get("/appointments", params={"patient_id": 1042}, headers=_auth())
        assert resp.status_code == 200, f"{role} should read appointments"


def test_reading_appointments_is_separable_from_booking_them(client, monkeypatch):
    # The grid distinguishes read from write. Nothing should collapse them.
    from roles_config import permissions_for

    assert "appointments.read" in permissions_for("management")
    assert "appointments.write" not in permissions_for("management")

    monkeypatch.setattr(app_mod, "get_session", lambda t: _session_for("management") if t == VALID_TOKEN else None)
    _stub_downstream(monkeypatch, payload={"ok": True})

    assert client.get("/appointments", params={"patient_id": 1}, headers=_auth()).status_code == 200
    assert client.post("/appointments", json={}, headers=_auth()).status_code == 403
