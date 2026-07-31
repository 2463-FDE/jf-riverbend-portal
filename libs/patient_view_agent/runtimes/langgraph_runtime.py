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
pattern, for the suite that runs with no install required; it validates
this module's own control flow against a documented shape of LangGraph's
API. Real compatibility against the exact pins in requirements-langgraph.txt
(`langgraph==0.2.62`, `langchain-core==0.3.29`) HAS been verified by hand —
the successful, refused, and denied paths all run correctly against the
real installed package, matching the faked-suite's behavior — and is
covered going forward by `tests/test_patient_view_langgraph_smoke.py`,
which skips (not fails) when the optional package isn't installed.

Dependency/framework-failure policy (explicit, per review): missing
`langgraph`, a version/API-shape mismatch in an installed one, or any other
framework-internal failure is **not** configuration-fatal at
`build_runtime("langgraph")` selection time — this class has no `__init__`
and touches nothing until a request actually runs `run()`. At request time,
authorization still happens first (see above); everything from the
`langgraph.graph` import through `compiled.invoke()` is wrapped in one
`except Exception`, degrading to a safe ESCALATED `PatientViewResult`
(`runtime.runtime_unavailable_result`, logging the exception TYPE only)
rather than propagating — this is the same "never raise downstream of
authorization" contract `PatientViewRuntime.run()` already documents for
evidence/composer failures, now also covering the runtime's own framework
plumbing. This is broader than `runtimes/custom.py`, which does NOT wrap its
specialist calls this defensively and would crash on a genuinely unexpected
bug (e.g. a broken custom repository) — a deliberate asymmetry: custom is
the trusted production default, where a bug should surface loudly; langgraph
is the optional, experimental comparison spike, where erring toward "never a
500" is the more defensible choice while it is still being evaluated.
The alternative (fail loudly at `build_runtime()` selection time) was
considered and rejected: the factory is a plain constructor with no request
context, so it cannot distinguish "about to serve real traffic" from
"constructed once at import time" — degrading per-request, after
authorization, is the point where a human-facing outcome (safe escalation,
not a 500) is actually possible.
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
from ..runtime import PatientViewResult, finalize_result, initial_reasons, refused_result, runtime_unavailable_result
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

        # Everything from the (deliberately post-authorization — see
        # "Dependency-absence policy" above) langgraph import through
        # invoke() is wrapped in one net: an authorized request must not
        # crash either, whether the failure is a missing optional dependency,
        # a version/API-shape mismatch in an installed one, or a framework
        # internal error — none of that is the caller's fault, and the
        # PatientViewRuntime contract promises no raise downstream of
        # authorization. This is a deliberate asymmetry with
        # runtimes/custom.py, which does NOT wrap its chart/graph specialist
        # calls this broadly and would crash on a genuinely unexpected bug
        # (e.g. a broken custom repository): custom is the trusted
        # production default, where a bug should surface loudly; langgraph is
        # the optional, experimental comparison spike, where erring toward
        # "never a 500" is the more defensible choice while it is still being
        # evaluated. Only `error_type` is logged, never a message or any
        # state — this is the one place an external library's own exception
        # text could otherwise leak into a PHI-safe log.
        try:
            from langgraph.graph import END, StateGraph

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
        except Exception as exc:
            return runtime_unavailable_result(
                correlation_id=scope.correlation_id,
                patient_id=scope.patient_id,
                error_type=type(exc).__name__,
                elapsed_seconds=clock() - start,
            )

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
