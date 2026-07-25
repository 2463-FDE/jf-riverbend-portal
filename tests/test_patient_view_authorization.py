"""Stage 2 — deterministic authorization boundary tests.

Covers: allow, deny-by-default (unknown actor), cross-patient deny, wrong
action, wrong purpose, correlation-id handling, the deny-before-read invariant
(zero repository reads on denial), and PHI-safe logging.
"""
import logging

import pytest

from libs.patient_view_agent import (
    Action,
    AuthorizationDenied,
    AuthorizationRequest,
    AuthorizedScope,
    DenialReason,
    FakePolicyAuthorization,
    GraphLimits,
    Purpose,
    SeededChartRepository,
    build_patient_graph,
    seed_derived_sample,
)

FIXED_CID = "corrid-deadbeef"


def make_authorizer(grants=None, **kw):
    return FakePolicyAuthorization(
        grants or {"frontdesk": {1042}}, id_factory=lambda: FIXED_CID, **kw
    )


def req(actor="frontdesk", patient=1042, action=Action.VIEW_PATIENT_CHART,
        purpose=Purpose.TREATMENT, cid=None):
    return AuthorizationRequest(
        actor_id=actor, patient_id=patient, action=action, purpose=purpose, correlation_id=cid
    )


def test_allow_returns_authorized_scope():
    scope = make_authorizer().authorize(req())
    assert isinstance(scope, AuthorizedScope)
    assert scope.actor_id == "frontdesk"
    assert scope.patient_id == 1042
    assert scope.action == Action.VIEW_PATIENT_CHART
    assert scope.purpose == Purpose.TREATMENT
    assert scope.correlation_id == FIXED_CID


def test_supplied_correlation_id_is_preserved():
    scope = make_authorizer().authorize(req(cid="caller-supplied-id"))
    assert scope.correlation_id == "caller-supplied-id"


def test_unknown_actor_is_denied_by_default():
    with pytest.raises(AuthorizationDenied) as ei:
        make_authorizer().authorize(req(actor="not-in-grant-table"))
    assert ei.value.denial.reason == DenialReason.UNKNOWN_ACTOR
    assert ei.value.denial.correlation_id == FIXED_CID


def test_cross_patient_request_is_denied():
    # frontdesk is granted only patient 1042; asking for 1043 must be refused.
    with pytest.raises(AuthorizationDenied) as ei:
        make_authorizer().authorize(req(patient=1043))
    assert ei.value.denial.reason == DenialReason.NOT_AUTHORIZED


def test_action_not_permitted_is_denied():
    authz = make_authorizer(allowed_actions=set())  # nothing allowed
    with pytest.raises(AuthorizationDenied) as ei:
        authz.authorize(req())
    assert ei.value.denial.reason == DenialReason.ACTION_NOT_PERMITTED


def test_purpose_not_permitted_is_denied():
    # default allowed_purposes is {TREATMENT}; PAYMENT must be refused.
    with pytest.raises(AuthorizationDenied) as ei:
        make_authorizer().authorize(req(purpose=Purpose.PAYMENT))
    assert ei.value.denial.reason == DenialReason.PURPOSE_NOT_PERMITTED


def test_denied_request_performs_zero_reads():
    encounters, records = seed_derived_sample()
    repo = SeededChartRepository(encounters, records)
    authorizer = make_authorizer({"frontdesk": {1042}})
    with pytest.raises(AuthorizationDenied):
        build_patient_graph(req(patient=1043), authorizer=authorizer, repository=repo)
    assert repo.load_calls == 0  # authorization raised before any read


def test_authorized_request_reads_exactly_once_and_returns_graph():
    encounters, records = seed_derived_sample()
    repo = SeededChartRepository(encounters, records)
    authorizer = make_authorizer({"frontdesk": {1042}})
    graph = build_patient_graph(req(patient=1042), authorizer=authorizer, repository=repo)
    assert repo.load_calls == 1
    assert graph.patient_id == 1042
    assert graph.correlation_id == FIXED_CID


def test_denial_and_allow_logs_contain_no_phi(caplog):
    encounters, records = seed_derived_sample()
    repo = SeededChartRepository(encounters, records)
    authorizer = make_authorizer({"frontdesk-user": {1042}})
    with caplog.at_level(logging.INFO):
        build_patient_graph(
            req(actor="frontdesk-user", patient=1042),
            authorizer=authorizer,
            repository=repo,
        )
        with pytest.raises(AuthorizationDenied):
            build_patient_graph(
                req(actor="frontdesk-user", patient=1043),
                authorizer=authorizer,
                repository=repo,
            )
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert blob.strip()  # something was logged
    assert "correlation_id=" in blob and "outcome=" in blob
    # No actor id, patient id, SSN, or patient name may appear in logs.
    for forbidden in ["1042", "1043", "frontdesk-user", "412-55-9981", "Maria", "Gonzalez", "O'Brien"]:
        assert forbidden not in blob, f"PHI/identifier leaked into logs: {forbidden!r}"
