"""Stage 3 — bounded supervisor and fixed specialist tests.

Covers: successful fan-out, zero-tool denial (zero reads), unknown-tool
rejection, maximum-turn (compose-attempt) termination, provider failure,
evidence mismatch (independent-read disagreement), missing evidence, human
escalation (non-treatment purpose), deterministic fake-provider output, and
PHI-safe logging.
"""
import logging

import pytest

from libs.llm_client.client import LLMClient, LLMConfig
from libs.llm_client.errors import ProviderTimeoutError
from libs.llm_client.providers.fake_provider import FakeProvider
from libs.patient_view_agent import (
    Action,
    AuthorizationDenied,
    AuthorizationRequest,
    ChartRepositoryPort,
    ChartResult,
    EncounterRow,
    EvidenceIntegrityError,
    FakePolicyAuthorization,
    PatientViewOutcome,
    Purpose,
    RecordRow,
    SeededChartRepository,
    SpecialistError,
    ViewReason,
    run_patient_view,
    seed_derived_sample,
)
from libs.patient_view_agent.composer import ComposedSummary, compose
from libs.patient_view_agent.specialists import run_specialist

FIXED_CID = "corrid-runtime"


def make_authorizer(grants=None, **kw):
    return FakePolicyAuthorization(grants or {"clinician": {1042, 1043, 5000}}, id_factory=lambda: FIXED_CID, **kw)


def req(actor="clinician", patient=1042, purpose=Purpose.TREATMENT):
    return AuthorizationRequest(actor_id=actor, patient_id=patient, action=Action.VIEW_PATIENT_CHART, purpose=purpose)


def fresh_repo():
    return SeededChartRepository(*seed_derived_sample())


# --------------------------------------------------------------------------- #
# Successful fan-out
# --------------------------------------------------------------------------- #


def test_successful_fan_out_completes_with_evidence_ids():
    repo = fresh_repo()
    result = run_patient_view(req(patient=1042), authorizer=make_authorizer(), repository=repo)

    assert result.outcome == PatientViewOutcome.COMPLETED
    assert result.escalation is False
    assert ViewReason.EVIDENCE_FOUND in result.reasons
    assert result.evidence_ids  # non-empty, all citable
    assert set(result.evidence_ids) <= {"patient:1042", "encounter:1", "encounter:6", "provider:dr-patel", "provider:dr-grace-kim", "record:1", "record:6"}
    assert result.execution.specialists_run == ["chart_read", "graph_read", "evidence_validate"]
    assert result.execution.reads == 4  # 2 (chart specialist) + 2 (graph specialist's own internal read)
    assert result.patient_id == 1042
    assert result.correlation_id == FIXED_CID


def test_cross_patient_data_never_appears_for_authorized_patient():
    repo = fresh_repo()
    result = run_patient_view(req(patient=1043), authorizer=make_authorizer(), repository=repo)
    assert result.outcome == PatientViewOutcome.COMPLETED
    # No 1042-only node ids leak into a 1043 view.
    assert "encounter:1" not in result.evidence_ids
    assert "encounter:6" not in result.evidence_ids
    assert {"encounter:4", "encounter:7"} <= set(result.evidence_ids)


# --------------------------------------------------------------------------- #
# Zero-tool denial
# --------------------------------------------------------------------------- #


def test_denied_request_performs_zero_reads_and_zero_specialists():
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})
    with pytest.raises(AuthorizationDenied):
        run_patient_view(req(patient=9999), authorizer=authorizer, repository=repo)
    assert repo.load_calls == 0


# --------------------------------------------------------------------------- #
# Unknown-tool rejection
# --------------------------------------------------------------------------- #


def test_unknown_tool_name_is_rejected_before_invocation():
    called = {"yes": False}

    def _would_leak():
        called["yes"] = True
        return "should never run"

    with pytest.raises(SpecialistError):
        run_specialist("delete_patient_record", _would_leak)
    assert called["yes"] is False


# --------------------------------------------------------------------------- #
# Missing evidence -> escalation
# --------------------------------------------------------------------------- #


def test_missing_evidence_escalates_rather_than_guessing():
    repo = fresh_repo()  # seed has no rows for patient 5000
    authorizer = make_authorizer({"clinician": {5000}})
    result = run_patient_view(req(patient=5000), authorizer=authorizer, repository=repo)

    assert result.outcome == PatientViewOutcome.ESCALATED
    assert result.escalation is True
    assert ViewReason.NO_EVIDENCE in result.reasons
    # Only the patient root node exists (no encounter/record evidence) — the
    # graph always includes it, so this is not the same as an empty list.
    assert result.evidence_ids == ["patient:5000"]


# --------------------------------------------------------------------------- #
# Human escalation for a non-treatment purpose
# --------------------------------------------------------------------------- #


def test_non_treatment_purpose_forces_escalation_even_with_evidence():
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}}, allowed_purposes={Purpose.TREATMENT, Purpose.PAYMENT})
    result = run_patient_view(req(patient=1042, purpose=Purpose.PAYMENT), authorizer=authorizer, repository=repo)

    assert result.outcome == PatientViewOutcome.ESCALATED
    assert result.escalation is True
    assert ViewReason.NON_TREATMENT_PURPOSE in result.reasons
    # Evidence was present and is still surfaced for human review, never hidden needlessly.
    assert result.evidence_ids


# --------------------------------------------------------------------------- #
# Evidence mismatch between independent chart/graph reads -> refusal
# --------------------------------------------------------------------------- #


class _FlakyRepo(ChartRepositoryPort):
    """Returns a legitimate chart on its first call (used by the chart
    specialist) and a chart with an extra, unsupported record on its second
    call (used internally by the graph specialist's reader) — simulating two
    independent reads that disagree. Proves the evidence validator catches
    this even though neither individual read looks internally wrong."""

    def __init__(self, patient_id: int):
        self._patient_id = patient_id
        self.load_calls = 0

    def load_chart(self, patient_id, *, correlation_id=""):
        self.load_calls += 1
        encounters = [EncounterRow(id=1, patient_id=patient_id, provider="Dr. Patel")]
        if self.load_calls == 1:
            records = []
        else:
            records = [RecordRow(id=999, encounter_id=1, patient_id=patient_id, kind="note", title="ghost", status="final")]
        return ChartResult(patient_id=patient_id, encounters=encounters, records=records, reads=2)


def test_evidence_mismatch_between_specialists_is_refused_not_shown():
    repo = _FlakyRepo(1042)
    authorizer = make_authorizer({"clinician": {1042}})
    result = run_patient_view(req(patient=1042), authorizer=authorizer, repository=repo)

    assert result.outcome == PatientViewOutcome.REFUSED
    assert result.escalation is True
    assert ViewReason.UNSUPPORTED_EVIDENCE in result.reasons
    assert result.evidence_ids == []
    assert "refused" in result.summary.lower()


def test_evidence_integrity_error_carries_no_phi():
    err = EvidenceIntegrityError("cid-1", [ViewReason.CROSS_PATIENT_EVIDENCE])
    assert str(err) == "evidence integrity check failed"
    assert err.correlation_id == "cid-1"


# --------------------------------------------------------------------------- #
# Composer: deterministic fake-provider output, provider failure, max-attempts
# --------------------------------------------------------------------------- #


def _evidence_for(patient_id=1042):
    from libs.patient_view_agent.authorization import FakePolicyAuthorization as _Authz
    from libs.patient_view_agent.contracts import AuthorizationRequest as _Req
    from libs.patient_view_agent.graph import PatientGraphReader
    from libs.patient_view_agent.specialists import validate_evidence

    repo = fresh_repo()
    authz = _Authz({"actor": {patient_id}}, id_factory=lambda: FIXED_CID)
    scope = authz.authorize(_Req(actor_id="actor", patient_id=patient_id, action=Action.VIEW_PATIENT_CHART, purpose=Purpose.TREATMENT))
    chart = repo.load_chart(patient_id, correlation_id=scope.correlation_id)
    graph = PatientGraphReader(scope, repo).build()
    return scope, validate_evidence(scope, chart, graph)


def test_compose_with_no_llm_client_uses_deterministic_template():
    scope, evidence = _evidence_for()
    result, attempts, used_fallback = compose(scope, evidence, llm_client=None)
    assert attempts == 0
    assert used_fallback is False
    assert set(result.cited_evidence_ids) == set(evidence.evidence_ids)
    assert str(scope.patient_id) in result.summary


def test_compose_returns_deterministic_fake_provider_output():
    scope, evidence = _evidence_for()
    valid_id = evidence.evidence_ids[0]
    script = [ComposedSummary(summary="Fixed deterministic summary.", cited_evidence_ids=[valid_id])]
    # FakeProvider script must be ProviderResponse/Exception, not a schema instance directly:
    from libs.llm_client.providers.base import ProviderResponse

    provider = FakeProvider([ProviderResponse(text=script[0].model_dump_json(), input_tokens=1, output_tokens=1)])
    client = LLMClient(config=LLMConfig(provider="fake"), provider=provider)

    result, attempts, used_fallback = compose(scope, evidence, llm_client=client)
    assert attempts == 1
    assert used_fallback is False
    assert result.summary == "Fixed deterministic summary."
    assert result.cited_evidence_ids == [valid_id]

    # Re-running with an identically-scripted fresh provider gives the same result — deterministic.
    provider2 = FakeProvider([ProviderResponse(text=script[0].model_dump_json(), input_tokens=1, output_tokens=1)])
    client2 = LLMClient(config=LLMConfig(provider="fake"), provider=provider2)
    result2, attempts2, used_fallback2 = compose(scope, evidence, llm_client=client2)
    assert result2 == result and attempts2 == attempts and used_fallback2 == used_fallback


def test_compose_falls_back_on_provider_error():
    scope, evidence = _evidence_for()
    provider = FakeProvider([ProviderTimeoutError("boom")])
    client = LLMClient(config=LLMConfig(provider="fake", max_retries=0), provider=provider)

    result, attempts, used_fallback = compose(scope, evidence, llm_client=client)
    assert used_fallback is True
    assert attempts == 1
    assert set(result.cited_evidence_ids) == set(evidence.evidence_ids)


def test_compose_hits_max_attempts_on_repeated_unsupported_citations():
    scope, evidence = _evidence_for()
    from libs.llm_client.providers.base import ProviderResponse

    bad = ComposedSummary(summary="hallucinated", cited_evidence_ids=["record:999999"])
    provider = FakeProvider(
        [
            ProviderResponse(text=bad.model_dump_json(), input_tokens=1, output_tokens=1),
            ProviderResponse(text=bad.model_dump_json(), input_tokens=1, output_tokens=1),
        ]
    )
    client = LLMClient(config=LLMConfig(provider="fake"), provider=provider)

    result, attempts, used_fallback = compose(scope, evidence, llm_client=client, max_attempts=2)
    assert attempts == 2  # bounded — never loops past max_attempts
    assert used_fallback is True
    assert set(result.cited_evidence_ids) == set(evidence.evidence_ids)
    assert len(provider.calls) == 2


def test_runtime_final_validator_rejects_composer_output_independently_of_compose():
    # Even if compose() itself somehow returned an unsupported citation (a
    # future bug in composer.py), the supervisor's own final validator must
    # still catch it — proven here by injecting a fake compose_fn directly.
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})

    def _malicious_compose_fn(scope, evidence, *, llm_client=None):
        return ComposedSummary(summary="untrustworthy", cited_evidence_ids=["record:999999"]), 1, False

    result = run_patient_view(req(patient=1042), authorizer=authorizer, repository=repo, compose_fn=_malicious_compose_fn)
    assert result.outcome == PatientViewOutcome.REFUSED
    assert ViewReason.UNSUPPORTED_EVIDENCE in result.reasons
    assert result.evidence_ids == []


# --------------------------------------------------------------------------- #
# Timeout budget
# --------------------------------------------------------------------------- #


def test_elapsed_time_budget_overrun_is_flagged_and_escalates():
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})
    ticks = iter([0.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])

    def fake_clock():
        return next(ticks, 100.0)

    result = run_patient_view(req(patient=1042), authorizer=authorizer, repository=repo, max_seconds=1.0, clock=fake_clock)
    assert ViewReason.TIMEOUT in result.reasons
    assert result.escalation is True
    assert result.outcome == PatientViewOutcome.ESCALATED


# --------------------------------------------------------------------------- #
# PHI-safe logging
# --------------------------------------------------------------------------- #


def test_full_run_logs_contain_no_phi(caplog):
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})
    with caplog.at_level(logging.INFO):
        run_patient_view(req(patient=1042), authorizer=authorizer, repository=repo)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for forbidden in ["Maria", "Gonzalez", "O'Brien", "412-55-9981", "clinician"]:
        assert forbidden not in blob, f"PHI/identifier leaked into logs: {forbidden!r}"
