"""Smoke test against the REAL, installed `langgraph`/`langchain-core`
packages — not the `sys.modules` fake `tests/test_patient_view_runtime_contract.py`
uses for its no-install-required suite.

Skipped, not failed, when the optional `libs/patient_view_agent/requirements-langgraph.txt`
dependencies aren't installed (the default everywhere in this repo, including
CI — `requirements-dev.txt` never installs them). Install them in a
disposable environment and run this file whenever
`runtimes/langgraph_runtime.py` changes, to actually verify the assumed
`StateGraph`/`compile`/`invoke` API shape still matches the pinned real
library — the parametrized contract suite's fake validates this module's
own control flow, not compatibility with the real package.

Run with:  pip install -r libs/patient_view_agent/requirements-langgraph.txt
           pytest tests/test_patient_view_langgraph_smoke.py -v
"""
import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from libs.patient_view_agent import (  # noqa: E402
    Action,
    AuthorizationDenied,
    AuthorizationRequest,
    ChartRepositoryPort,
    ChartResult,
    EncounterRow,
    FakePolicyAuthorization,
    PatientViewOutcome,
    Purpose,
    RecordRow,
    SeededChartRepository,
    ViewReason,
    build_runtime,
    seed_derived_sample,
)

FIXED_CID = "corrid-langgraph-smoke"


def make_authorizer(grants=None, **kw):
    return FakePolicyAuthorization(grants or {"clinician": {1042}}, id_factory=lambda: FIXED_CID, **kw)


def req(actor="clinician", patient=1042, purpose=Purpose.TREATMENT):
    return AuthorizationRequest(actor_id=actor, patient_id=patient, action=Action.VIEW_PATIENT_CHART, purpose=purpose)


class _FlakyRepo(ChartRepositoryPort):
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


def test_real_langgraph_completes_a_successful_request():
    repo = SeededChartRepository(*seed_derived_sample())
    result = build_runtime("langgraph").run(req(patient=1042), authorizer=make_authorizer(), repository=repo)

    assert result.outcome == PatientViewOutcome.COMPLETED
    assert result.execution.specialists_run == ["chart_read", "graph_read", "evidence_validate"]
    assert result.evidence_ids


def test_real_langgraph_refuses_on_evidence_mismatch():
    result = build_runtime("langgraph").run(
        req(patient=1042), authorizer=make_authorizer(), repository=_FlakyRepo(1042)
    )

    assert result.outcome == PatientViewOutcome.REFUSED
    assert ViewReason.UNSUPPORTED_EVIDENCE in result.reasons
    assert result.evidence_ids == []


def test_real_langgraph_denies_before_any_read():
    repo = SeededChartRepository(*seed_derived_sample())
    authorizer = make_authorizer({"clinician": {1042}})

    with pytest.raises(AuthorizationDenied):
        build_runtime("langgraph").run(req(patient=9999), authorizer=authorizer, repository=repo)
    assert repo.load_calls == 0
