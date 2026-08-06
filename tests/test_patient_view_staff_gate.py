"""Stage 3 — StaffAccessGate: an authenticated-staff access gate, NOT
patient-specific authorization.

Mirrors tests/test_patient_view_authorization.py's coverage shape (allow,
deny-by-default, deny-before-read, PHI-safe logs) but for the real gate wired
into services/records-service/app.py, whose defining property is different
from FakePolicyAuthorization's: it has no per-actor grant table at all, so
ANY non-empty actor_id is allowed for ANY patient_id.
"""
import logging

import pytest

from libs.patient_view_agent import (
    Action,
    AuthorizationDenied,
    AuthorizationRequest,
    DenialReason,
    Purpose,
    SeededChartRepository,
    StaffAccessGate,
    build_patient_graph,
    seed_derived_sample,
)
from libs.patient_view_agent.contracts import AuthorizedScope

FIXED_CID = "corrid-staffgate"


def gate(**kw):
    return StaffAccessGate(id_factory=lambda: FIXED_CID, **kw)


def req(actor="frontdesk", patient=1042, action=Action.VIEW_PATIENT_CHART,
        purpose=Purpose.TREATMENT, cid=None):
    return AuthorizationRequest(
        actor_id=actor, patient_id=patient, action=action, purpose=purpose, correlation_id=cid
    )


def test_authenticated_actor_is_allowed_for_any_patient_id():
    # The defining property: no grant table. Two different patients, same
    # actor, both ALLOW — because this is a staff-authentication check, not
    # a per-patient ownership check.
    scope_a = gate().authorize(req(actor="frontdesk", patient=1042))
    scope_b = gate().authorize(req(actor="frontdesk", patient=1043))
    assert isinstance(scope_a, AuthorizedScope)
    assert isinstance(scope_b, AuthorizedScope)
    assert scope_a.patient_id == 1042
    assert scope_b.patient_id == 1043


def test_different_actors_both_allowed_same_patient():
    # Confirms this is NOT patient-specific: two unrelated actors can both
    # view the same patient_id with no ownership fact checked either way.
    scope_a = gate().authorize(req(actor="frontdesk", patient=1042))
    scope_b = gate().authorize(req(actor="billing-clerk", patient=1042))
    assert scope_a.actor_id == "frontdesk"
    assert scope_b.actor_id == "billing-clerk"


def test_empty_actor_id_is_denied():
    with pytest.raises(AuthorizationDenied) as ei:
        gate().authorize(req(actor=""))
    assert ei.value.denial.reason == DenialReason.UNKNOWN_ACTOR
    assert ei.value.denial.correlation_id == FIXED_CID


def test_action_not_permitted_is_denied():
    authz = gate(allowed_actions=set())
    with pytest.raises(AuthorizationDenied) as ei:
        authz.authorize(req())
    assert ei.value.denial.reason == DenialReason.ACTION_NOT_PERMITTED


def test_purpose_not_permitted_is_denied():
    authz = gate(allowed_purposes=set())
    with pytest.raises(AuthorizationDenied) as ei:
        authz.authorize(req())
    assert ei.value.denial.reason == DenialReason.PURPOSE_NOT_PERMITTED


def test_supplied_correlation_id_is_preserved():
    scope = gate().authorize(req(cid="caller-supplied-id"))
    assert scope.correlation_id == "caller-supplied-id"


def test_denied_request_performs_zero_reads():
    encounters, records = seed_derived_sample()
    repo = SeededChartRepository(encounters, records)
    with pytest.raises(AuthorizationDenied):
        build_patient_graph(req(actor=""), authorizer=gate(), repository=repo)
    assert repo.load_calls == 0


def test_allowed_request_reads_and_returns_graph():
    encounters, records = seed_derived_sample()
    repo = SeededChartRepository(encounters, records)
    graph = build_patient_graph(req(patient=1042), authorizer=gate(), repository=repo)
    assert repo.load_calls == 1
    assert graph.patient_id == 1042


def test_allow_and_deny_logs_contain_no_phi(caplog):
    encounters, records = seed_derived_sample()
    repo = SeededChartRepository(encounters, records)
    with caplog.at_level(logging.INFO):
        build_patient_graph(req(actor="frontdesk-user", patient=1042), authorizer=gate(), repository=repo)
        with pytest.raises(AuthorizationDenied):
            build_patient_graph(req(actor=""), authorizer=gate(), repository=repo)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert blob.strip()
    assert "correlation_id=" in blob and "outcome=" in blob
    for forbidden in ["1042", "frontdesk-user", "412-55-9981", "Maria", "Gonzalez"]:
        assert forbidden not in blob, f"PHI/identifier leaked into logs: {forbidden!r}"
