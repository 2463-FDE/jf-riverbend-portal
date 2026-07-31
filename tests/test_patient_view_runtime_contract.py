"""Week 5 — contract-parity tests for the two switchable PatientViewRuntime
implementations (libs/patient_view_agent/runtimes/{custom,langgraph_runtime}.py).

The SAME test functions run against both runtimes via the `runtime_name`
fixture — mirrors tests/test_eligibility_agent_runtimes.py's established
shape — so a passing suite is evidence the *contract* holds for each
implementation, not just that each does something plausible on its own.

langgraph/langchain_core are faked via sys.modules (same pattern as
test_eligibility_agent_runtimes.py's `_install_fake_langgraph`): no real
install is required, and this validates
runtimes/langgraph_runtime.py's own control flow against a documented shape
of LangGraph's StateGraph/conditional-edges API, not compatibility with the
real library.
"""
import logging
import sys
import types

import pytest

from libs.patient_view_agent import (
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

FIXED_CID = "corrid-contract"


# --------------------------------------------------------------------------- #
# Fake langgraph — mirrors test_eligibility_agent_runtimes.py's
# _install_fake_langgraph, minus langchain_core.messages (this runtime never
# uses LangChain message/chat-model types — composer.py calls libs.llm_client
# directly, not a bound chat model).
# --------------------------------------------------------------------------- #


def _install_fake_langgraph(monkeypatch):
    recorded = {"checkpointers": [], "node_names": set()}
    _END = object()

    class _FakeStateGraph:
        def __init__(self, schema):
            self._nodes = {}
            self._entry = None
            self._edges = {}
            self._cond_edges = {}

        def add_node(self, name, fn):
            self._nodes[name] = fn
            recorded["node_names"].add(name)

        def set_entry_point(self, name):
            self._entry = name

        def add_edge(self, source, target):
            self._edges[source] = target

        def add_conditional_edges(self, source, router, mapping):
            self._cond_edges[source] = (router, mapping)

        def compile(self, checkpointer=None):
            recorded["checkpointers"].append(checkpointer)
            nodes, entry, edges, cond_edges = self._nodes, self._entry, self._edges, self._cond_edges

            class _FakeCompiledGraph:
                def invoke(self, state, config=None):
                    current = entry
                    while True:
                        state = nodes[current](state)
                        if current in cond_edges:
                            router, mapping = cond_edges[current]
                            target = mapping[router(state)]
                        else:
                            target = edges.get(current, _END)
                        if target is _END:
                            return state
                        current = target

            return _FakeCompiledGraph()

    fake_graph_mod = types.ModuleType("langgraph.graph")
    fake_graph_mod.StateGraph = _FakeStateGraph
    fake_graph_mod.END = _END
    fake_langgraph_mod = types.ModuleType("langgraph")

    monkeypatch.setitem(sys.modules, "langgraph", fake_langgraph_mod)
    monkeypatch.setitem(sys.modules, "langgraph.graph", fake_graph_mod)
    return recorded


@pytest.fixture(params=["custom", "langgraph"])
def runtime_name(request, monkeypatch):
    if request.param == "langgraph":
        _install_fake_langgraph(monkeypatch)
    return request.param


def runtime(runtime_name):
    return build_runtime(runtime_name)


# --------------------------------------------------------------------------- #
# Shared fixtures (mirrors tests/test_patient_view_runtime.py)
# --------------------------------------------------------------------------- #


def make_authorizer(grants=None, **kw):
    return FakePolicyAuthorization(grants or {"clinician": {1042, 1043, 5000}}, id_factory=lambda: FIXED_CID, **kw)


def req(actor="clinician", patient=1042, purpose=Purpose.TREATMENT):
    return AuthorizationRequest(actor_id=actor, patient_id=patient, action=Action.VIEW_PATIENT_CHART, purpose=purpose)


def fresh_repo():
    return SeededChartRepository(*seed_derived_sample())


class _FlakyRepo(ChartRepositoryPort):
    """Returns a legitimate chart on its first call (the chart specialist)
    and a chart with an extra, unsupported record on its second call (the
    graph specialist's own internal read) — two independent reads that
    disagree. Proves the evidence validator catches this regardless of which
    runtime dispatches the two reads."""

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


# --------------------------------------------------------------------------- #
# Contract tests — run once per runtime_name
# --------------------------------------------------------------------------- #


def test_successful_fan_out_completes_with_evidence_ids(runtime_name):
    repo = fresh_repo()
    result = runtime(runtime_name).run(req(patient=1042), authorizer=make_authorizer(), repository=repo)

    assert result.outcome == PatientViewOutcome.COMPLETED
    assert result.escalation is False
    assert ViewReason.EVIDENCE_FOUND in result.reasons
    assert result.evidence_ids
    assert result.execution.specialists_run == ["chart_read", "graph_read", "evidence_validate"]
    assert result.execution.reads == 4
    assert result.patient_id == 1042
    assert result.correlation_id == FIXED_CID


def test_cross_patient_data_never_appears_for_authorized_patient(runtime_name):
    repo = fresh_repo()
    result = runtime(runtime_name).run(req(patient=1043), authorizer=make_authorizer(), repository=repo)
    assert result.outcome == PatientViewOutcome.COMPLETED
    assert "encounter:1" not in result.evidence_ids
    assert "encounter:6" not in result.evidence_ids
    assert {"encounter:4", "encounter:7"} <= set(result.evidence_ids)


def test_denied_request_performs_zero_reads(runtime_name):
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})
    with pytest.raises(AuthorizationDenied):
        runtime(runtime_name).run(req(patient=9999), authorizer=authorizer, repository=repo)
    assert repo.load_calls == 0


def test_missing_evidence_escalates_rather_than_guessing(runtime_name):
    repo = fresh_repo()  # seed has no rows for patient 5000
    authorizer = make_authorizer({"clinician": {5000}})
    result = runtime(runtime_name).run(req(patient=5000), authorizer=authorizer, repository=repo)

    assert result.outcome == PatientViewOutcome.ESCALATED
    assert result.escalation is True
    assert ViewReason.NO_EVIDENCE in result.reasons
    assert result.evidence_ids == ["patient:5000"]


def test_non_treatment_purpose_forces_escalation_even_with_evidence(runtime_name):
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}}, allowed_purposes={Purpose.TREATMENT, Purpose.PAYMENT})
    result = runtime(runtime_name).run(req(patient=1042, purpose=Purpose.PAYMENT), authorizer=authorizer, repository=repo)

    assert result.outcome == PatientViewOutcome.ESCALATED
    assert result.escalation is True
    assert ViewReason.NON_TREATMENT_PURPOSE in result.reasons
    assert result.evidence_ids


def test_evidence_mismatch_between_specialists_is_refused_not_shown(runtime_name):
    repo = _FlakyRepo(1042)
    authorizer = make_authorizer({"clinician": {1042}})
    result = runtime(runtime_name).run(req(patient=1042), authorizer=authorizer, repository=repo)

    assert result.outcome == PatientViewOutcome.REFUSED
    assert result.escalation is True
    assert ViewReason.UNSUPPORTED_EVIDENCE in result.reasons
    assert result.evidence_ids == []
    assert "refused" in result.summary.lower()


def test_elapsed_time_budget_overrun_is_flagged_and_escalates(runtime_name):
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})
    ticks = iter([0.0, 100.0, 100.0, 100.0])

    def fake_clock():
        return next(ticks, 100.0)

    result = runtime(runtime_name).run(
        req(patient=1042), authorizer=authorizer, repository=repo, max_seconds=1.0, clock=fake_clock
    )
    assert ViewReason.TIMEOUT in result.reasons
    assert result.escalation is True
    assert result.outcome == PatientViewOutcome.ESCALATED


def test_full_run_logs_contain_no_phi(runtime_name, caplog):
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})
    with caplog.at_level(logging.INFO):
        runtime(runtime_name).run(req(patient=1042), authorizer=authorizer, repository=repo)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    for forbidden in ["Maria", "Gonzalez", "O'Brien", "412-55-9981", "clinician"]:
        assert forbidden not in blob, f"PHI/identifier leaked into logs: {forbidden!r}"


# --------------------------------------------------------------------------- #
# Equivalence: both runtimes must produce the SAME PatientViewResult
# --------------------------------------------------------------------------- #


def _fixed_clock():
    ticks = iter([0.0, 1.0])
    return lambda: next(ticks)


def test_both_runtimes_produce_an_identical_result_for_the_same_seeded_input(monkeypatch):
    _install_fake_langgraph(monkeypatch)

    custom_result = build_runtime("custom").run(
        req(patient=1042), authorizer=make_authorizer(), repository=fresh_repo(), clock=_fixed_clock()
    )
    langgraph_result = build_runtime("langgraph").run(
        req(patient=1042), authorizer=make_authorizer(), repository=fresh_repo(), clock=_fixed_clock()
    )

    assert custom_result == langgraph_result


def test_both_runtimes_produce_an_identical_refusal_for_a_flaky_repo(monkeypatch):
    _install_fake_langgraph(monkeypatch)

    custom_result = build_runtime("custom").run(
        req(patient=1042), authorizer=make_authorizer(), repository=_FlakyRepo(1042), clock=_fixed_clock()
    )
    langgraph_result = build_runtime("langgraph").run(
        req(patient=1042), authorizer=make_authorizer(), repository=_FlakyRepo(1042), clock=_fixed_clock()
    )

    assert custom_result == langgraph_result


# --------------------------------------------------------------------------- #
# LangGraph-specific: no tool-calling agent, no checkpointer / no PHI at rest
# --------------------------------------------------------------------------- #


def test_langgraph_runtime_has_no_tool_calling_agent_node(monkeypatch):
    # Every node is a fixed, hardcoded function — no node whose job is to let
    # a model pick a tool. If a future edit accidentally added an
    # agent/tool-calling node, this fails.
    recorded = _install_fake_langgraph(monkeypatch)
    repo = fresh_repo()

    build_runtime("langgraph").run(req(patient=1042), authorizer=make_authorizer(), repository=repo)

    assert recorded["node_names"] == {"chart", "graph_read", "evidence", "compose", "final_validate"}


def test_langgraph_runtime_compiles_with_no_checkpointer(monkeypatch):
    # A checkpointer would persist graph state — including evidence ids — as
    # a new PHI-at-rest surface. Must always be None, never an in-memory or
    # backed saver.
    recorded = _install_fake_langgraph(monkeypatch)
    repo = fresh_repo()

    build_runtime("langgraph").run(req(patient=1042), authorizer=make_authorizer(), repository=repo)

    assert recorded["checkpointers"] == [None]


def test_langgraph_runtime_denies_before_the_graph_is_even_built(monkeypatch):
    # Authorization happens outside the graph — a DENY must never construct
    # or compile a graph at all, let alone invoke one.
    recorded = _install_fake_langgraph(monkeypatch)
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})

    with pytest.raises(AuthorizationDenied):
        build_runtime("langgraph").run(req(patient=9999), authorizer=authorizer, repository=repo)

    assert recorded["node_names"] == set()
    assert recorded["checkpointers"] == []
    assert repo.load_calls == 0


def test_langgraph_runtime_denies_before_importing_langgraph_at_all():
    # Regression test for a review finding: authorize() must run BEFORE the
    # `from langgraph.graph import ...` line, not just before the graph is
    # built — otherwise a denied request on an installation that hasn't
    # opted into requirements-langgraph.txt (the default: langgraph is
    # optional and uninstalled) fails with ModuleNotFoundError instead of
    # AuthorizationDenied, silently losing the deny-first guarantee. This
    # deliberately does NOT fake langgraph (unlike every other langgraph
    # test in this file) — it proves the real import order against the real
    # absence of the package, which a faked sys.modules entry can never
    # exercise (the import always "succeeds" against a fake).
    assert "langgraph" not in sys.modules, "langgraph must not already be imported for this test to mean anything"
    repo = fresh_repo()
    authorizer = make_authorizer({"clinician": {1042}})

    with pytest.raises(AuthorizationDenied):
        build_runtime("langgraph").run(req(patient=9999), authorizer=authorizer, repository=repo)

    assert repo.load_calls == 0
    assert "langgraph" not in sys.modules  # denial short-circuited before the import ever ran


# --------------------------------------------------------------------------- #
# Factory: fail-closed, config-only selection
# --------------------------------------------------------------------------- #


def test_build_runtime_rejects_unknown_name():
    with pytest.raises(ValueError, match="Unknown PATIENT_VIEW_RUNTIME"):
        build_runtime("autopilot")


def test_build_runtime_fails_closed_on_unrecognized_env_value(monkeypatch):
    monkeypatch.setenv("PATIENT_VIEW_RUNTIME", "some-typo")
    with pytest.raises(ValueError, match="Unknown PATIENT_VIEW_RUNTIME"):
        build_runtime()


def test_build_runtime_defaults_to_custom(monkeypatch):
    monkeypatch.delenv("PATIENT_VIEW_RUNTIME", raising=False)
    from libs.patient_view_agent.runtimes.custom import CustomPatientViewRuntime

    assert isinstance(build_runtime(), CustomPatientViewRuntime)


def test_langgraph_runtime_is_never_imported_when_custom_is_selected():
    # Importing/selecting "custom" must not require langgraph installed —
    # proven by NOT faking it here and confirming build_runtime("custom")
    # still works.
    assert sys.modules.get("langgraph") is None
    runtime_obj = build_runtime("custom")
    result = runtime_obj.run(req(patient=1042), authorizer=make_authorizer(), repository=fresh_repo())
    assert result.outcome == PatientViewOutcome.COMPLETED
