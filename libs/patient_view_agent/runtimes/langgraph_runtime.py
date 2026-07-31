"""Optional, reversible LangGraph comparison spike for the patient-view
supervisor — see docs/analysis/W5-orchestration-framework-evaluation.md §8.

A `StateGraph` mirroring the exact fixed sequence
`runtimes/custom.py::CustomPatientViewRuntime` runs sequentially:

    chart -> graph_read -> evidence -> (refused? END) -> compose
    -> final_validate -> END

Authorization happens BEFORE the graph is built or invoked, exactly like the
custom runtime: `authorizer.authorize(request)` raises `AuthorizationDenied`
here, so the graph is never even constructed on a DENY decision, and zero
reads occur. The minted `AuthorizedScope` is captured by closure and passed
to every node — it is never put into graph state, so no node could overwrite
it even by accident (LangGraph's shared-state model has no immutability
guarantee of its own; not putting scope in state is what keeps that
guarantee here).

Every node is a fixed, hardcoded function — there is no tool-calling agent
and no node whose behavior depends on model output, except `compose`, whose
only affordance is phrasing already-validator-approved evidence (identical
to the custom runtime; see composer.py). `compose`'s own bounded retry loop
lives inside `composer.compose()`, not as graph edges — the graph itself has
no cycles. The compiled graph has **no checkpointer** (`compile()` is called
with none): a checkpointer would persist state — including evidence ids —
as a new PHI-at-rest surface, which this deliverable's Stage 3 spec forbids
regardless of any other tradeoff. `recursion_limit` is set explicitly on
`invoke()` as a defensive bound even though this graph has no loops.

Both the refused-early-exit path and the final path call the SAME
`runtime.refused_result`/`runtime.finalize_result` helpers the custom runtime
calls — not independently-reimplemented equivalent logic, but literally the
same functions, which is what makes the contract suite's equivalence test a
structural guarantee rather than a coincidence.

`langgraph`/`langchain_core` imports are lazy (inside `run()`, never at
module scope), so importing this module — or this whole package — never
requires them installed; see requirements-langgraph.txt (optional,
uninstalled by default, never in requirements-dev.txt or a service).
tests/test_patient_view_runtime_contract.py fakes both via `sys.modules`,
mirroring libs/eligibility_agent/runtimes/langchain_runtime.py's established
pattern; it validates this module's own control flow against a documented
shape of LangGraph's API, not compatibility with the real library — this has
never been run against a real langgraph install, by design.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from libs.llm_client.client import LLMClient
from libs.safe_logging import get_safe_logger

from ..authorization import AuthorizationPort
from ..composer import compose as _default_compose
from ..contracts import AuthorizationRequest, GraphLimits
from ..graph import PatientGraphReader
from ..repository import ChartRepositoryPort
from ..runtime import PatientViewResult, finalize_result, initial_reasons, refused_result
from ..specialists import (
    CHART_READ_TOOL,
    EVIDENCE_VALIDATE_TOOL,
    GRAPH_READ_TOOL,
    EvidenceIntegrityError,
    ViewReason,
    run_specialist,
    validate_evidence,
)

log = get_safe_logger(__name__)

_RECURSION_LIMIT = 10  # this graph has no cycles; a small explicit bound is defense in depth, not a real ceiling


class LangGraphPatientViewRuntime:
    def run(
        self,
        request: AuthorizationRequest,
        *,
        authorizer: AuthorizationPort,
        repository: ChartRepositoryPort,
        limits: Optional[GraphLimits] = None,
        llm_client: Optional[LLMClient] = None,
        compose_fn: Callable = _default_compose,
        max_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> PatientViewResult:
        start = clock()
        scope = authorizer.authorize(request)  # raises AuthorizationDenied; zero reads before this line, no graph built

        # Deliberately imported AFTER authorization, not just lazily inside
        # run(): LangGraph is optional and uninstalled by default, so a
        # denied request must raise AuthorizationDenied, never
        # ModuleNotFoundError. Importing before the authorize() call would
        # turn every denial into a dependency crash on any installation that
        # hasn't opted into requirements-langgraph.txt — silently losing the
        # deny-first guarantee this runtime's docstring promises.
        from langgraph.graph import END, StateGraph

        specialists_run: list[str] = []

        def chart_node(state):
            state["chart"] = run_specialist(
                CHART_READ_TOOL, repository.load_chart, scope.patient_id, correlation_id=scope.correlation_id
            )
            specialists_run.append(CHART_READ_TOOL)
            return state

        def graph_node(state):
            state["graph"] = run_specialist(GRAPH_READ_TOOL, PatientGraphReader(scope, repository, limits=limits).build)
            specialists_run.append(GRAPH_READ_TOOL)
            return state

        def evidence_node(state):
            try:
                state["evidence"] = run_specialist(
                    EVIDENCE_VALIDATE_TOOL, validate_evidence, scope, state["chart"], state["graph"]
                )
                specialists_run.append(EVIDENCE_VALIDATE_TOOL)
            except EvidenceIntegrityError as exc:
                # Deliberately NOT appended to specialists_run — the call was
                # rejected, not completed. refused_result() still counts it
                # in tool_calls (specialists_run + 1), matching the custom
                # runtime's identical accounting exactly.
                state["refused"] = True
                state["refuse_reasons"] = exc.reasons
            return state

        def route_after_evidence(state):
            return "end" if state.get("refused") else "compose"

        def compose_node(state):
            evidence = state["evidence"]
            reasons = initial_reasons(request, evidence)
            composed, attempts, used_fallback = compose_fn(scope, evidence, llm_client=llm_client)
            if used_fallback:
                reasons.append(ViewReason.COMPOSE_FELL_BACK)
            state["composed"] = composed
            state["compose_attempts"] = attempts
            state["reasons"] = reasons
            return state

        def final_validate_node(state):
            state["elapsed_seconds"] = clock() - start
            return state

        graph = StateGraph(dict)
        graph.add_node("chart", chart_node)
        graph.add_node("graph_read", graph_node)
        graph.add_node("evidence", evidence_node)
        graph.add_node("compose", compose_node)
        graph.add_node("final_validate", final_validate_node)
        graph.set_entry_point("chart")
        graph.add_edge("chart", "graph_read")
        graph.add_edge("graph_read", "evidence")
        graph.add_conditional_edges("evidence", route_after_evidence, {"compose": "compose", "end": END})
        graph.add_edge("compose", "final_validate")
        graph.add_edge("final_validate", END)
        compiled = graph.compile(checkpointer=None)  # no PHI at rest — see module docstring

        final_state = compiled.invoke({}, config={"recursion_limit": _RECURSION_LIMIT})

        chart = final_state["chart"]
        graph_result = final_state["graph"]

        if final_state.get("refused"):
            return refused_result(
                correlation_id=scope.correlation_id,
                patient_id=scope.patient_id,
                specialists_run=specialists_run,
                reasons=final_state["refuse_reasons"],
                chart_reads=chart.reads,
                graph_reads=graph_result.reads,
                chart_truncated=chart.truncated,
                graph_truncated=graph_result.truncated,
                elapsed_seconds=clock() - start,
            )

        return finalize_result(
            correlation_id=scope.correlation_id,
            patient_id=scope.patient_id,
            specialists_run=specialists_run,
            evidence=final_state["evidence"],
            composed=final_state["composed"],
            compose_attempts=final_state["compose_attempts"],
            reasons=final_state["reasons"],
            chart_reads=chart.reads,
            graph_reads=graph_result.reads,
            chart_truncated=chart.truncated,
            graph_truncated=graph_result.truncated,
            elapsed_seconds=final_state["elapsed_seconds"],
            max_seconds=max_seconds,
        )
