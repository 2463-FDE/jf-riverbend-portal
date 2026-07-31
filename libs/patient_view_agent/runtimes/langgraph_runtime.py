"""Optional, reversible LangGraph comparison spike for the patient-view
supervisor — see docs/analysis/W5-orchestration-framework-evaluation.md §8.

A `StateGraph` mirroring the exact fixed sequence
`runtimes/custom.py::CustomPatientViewRuntime` runs sequentially:

    chart -> graph_read -> (refused? END) -> evidence -> (refused? END)
    -> compose -> final_validate -> END

`graph_read` has its own conditional exit (not just `evidence`): the graph
specialist's OWN internal cross-patient tripwire (`graph.py`'s
`CrossPatientEvidenceError`, raised from inside `PatientGraphReader.build()`)
is a distinct integrity failure from the evidence validator's
`EvidenceIntegrityError` and is caught right where it's raised, not by the
generic `compiled.invoke()` catch-all below — see "Dependency/framework-
failure policy" for why that catch-all must never see it.

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
authorization still happens first (see above); every failure downstream of
that degrades to a safe ESCALATED `PatientViewResult` rather than
propagating — the same "never raise downstream of authorization" contract
`PatientViewRuntime.run()` already documents for evidence/composer failures,
now also covering the runtime's own framework plumbing. This is split into
TWO separate nets, not one, because they have different accounting
guarantees:

- Import + graph construction + `compile()` — no node function has run yet,
  so zero reads have occurred BY CONSTRUCTION. `runtime.runtime_unavailable_result`
  hardcodes empty/zero metadata, which is correct here.
- `compiled.invoke()` — this is where node functions actually run and
  perform real reads, so a failure here (a node raising unexpectedly, or a
  framework-internal error inside invoke's own dispatch) must report
  whatever `specialists_run`/reads already completed via
  `runtime.node_failure_result`, not hardcoded zeros — an earlier version of
  this fix used ONE net covering both, which could report "zero reads" after
  `chart_node` had already performed a real one. `chart`/`graph_result` are
  captured in a `completed` dict by closure (not read from graph state,
  which invoke() never returns on failure) for exactly this reason.

`compiled.invoke()`'s catch-all is deliberately generic (`except Exception`)
because it exists to catch truly UNEXPECTED node/framework failures — it must
NOT be where a KNOWN, already-classified integrity failure gets reported.
`graph.py`'s `CrossPatientEvidenceError` (raised from inside
`PatientGraphReader.build()` when a row/node's patient id disagrees with the
authorized scope — a real potential cross-patient leak, per RIV-201) is
caught explicitly inside `graph_node`, one level below the generic net, and
routed to `runtime.refused_result` with `ViewReason.CROSS_PATIENT_EVIDENCE` —
the same reason `evidence_node`'s sibling `EvidenceIntegrityError` handling
already uses for the analogous chart/graph-disagreement case. An earlier
version of this runtime let `CrossPatientEvidenceError` fall through to the
generic `invoke()` catch-all, which reported it as a generic ESCALATED/
NODE_FAILURE — masking a security-relevant integrity rejection from any
audit/recovery code that keys off the refusal reason.

`runtimes/custom.py` now enforces the identical contract (an outer
`except Exception` around its own sequential authorize->chart->graph->
evidence->compose->finalize flow, added in the same review round that added
these two nets here) — an earlier version of this docstring claimed a
deliberate asymmetry ("custom crashes loudly, langgraph never does"), but
that was never actually licensed by `PatientViewRuntime.run()`'s own
contract, which promises nothing downstream of authorization may raise for
EITHER runtime. `custom.py` crashing on a plain repository error was a real,
unfixed gap, not an intentional design choice.
The alternative (fail loudly at `build_runtime()` selection time) was
considered and rejected: the factory is a plain constructor with no request
context, so it cannot distinguish "about to serve real traffic" from
"constructed once at import time" — degrading per-request, after
authorization, is the point where a human-facing outcome (safe escalation,
not a 500) is actually possible. Only `error_type` is logged in either net,
never a message — this is the one place an external library's own exception
text could otherwise leak into a PHI-safe log.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from libs.llm_client.client import LLMClient
from libs.safe_logging import get_safe_logger

from ..authorization import AuthorizationPort
from ..composer import compose as _default_compose
from ..contracts import AuthorizationRequest, GraphLimits
from ..graph import CrossPatientEvidenceError, PatientGraphReader
from ..repository import ChartRepositoryPort
from ..runtime import (
    PatientViewResult,
    finalize_result,
    initial_reasons,
    node_failure_result,
    refused_result,
    runtime_unavailable_result,
)
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
        # Mirrors specialists_run: captured by closure, not read from graph
        # state, so it survives even if a LATER node raises and invoke()
        # never returns any state at all — this is what lets the failure
        # path below report reads that actually happened instead of
        # silently claiming zero.
        completed: dict = {}

        def chart_node(state):
            chart = run_specialist(
                CHART_READ_TOOL, repository.load_chart, scope.patient_id, correlation_id=scope.correlation_id
            )
            completed["chart"] = chart
            state["chart"] = chart
            specialists_run.append(CHART_READ_TOOL)
            return state

        def graph_node(state):
            try:
                graph_result = run_specialist(
                    GRAPH_READ_TOOL, PatientGraphReader(scope, repository, limits=limits).build
                )
            except CrossPatientEvidenceError as exc:
                # The graph specialist's OWN internal cross-patient tripwire
                # (graph.py's `_reject`) fired — this is a security-relevant
                # integrity failure, not a generic node failure, and must be
                # classified as ViewReason.CROSS_PATIENT_EVIDENCE so audit
                # and recovery paths that key off the refusal reason still
                # see it. Deliberately NOT appended to specialists_run, same
                # as evidence_node's EvidenceIntegrityError handling below:
                # the call was rejected, not completed. `exc.reads`/
                # `exc.truncated` carry the repository read that already
                # happened inside `PatientGraphReader.build()` before it
                # rejected — stashed in state (there is no `PatientGraph`
                # object to read them from) so the refusal below doesn't
                # understate access in the audit trail.
                state["refused"] = True
                state["refuse_reasons"] = [ViewReason.CROSS_PATIENT_EVIDENCE]
                state["graph_reads"] = exc.reads
                state["graph_truncated"] = exc.truncated
                return state
            completed["graph"] = graph_result
            state["graph"] = graph_result
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

        def route_after_graph_read(state):
            return "end" if state.get("refused") else "evidence"

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

        # Import + graph construction + compile are wrapped in their own net,
        # separate from invoke() below: nothing here ever calls a node
        # function, so zero reads can have occurred by construction —
        # runtime_unavailable_result()'s hardcoded zero/empty metadata is
        # exactly correct for this failure class (missing optional
        # dependency, a version/API-shape mismatch, a compile-time framework
        # error). Only `error_type` is logged, never a message — this is the
        # one place an external library's own exception text could otherwise
        # leak into a PHI-safe log.
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
            graph.add_conditional_edges("graph_read", route_after_graph_read, {"evidence": "evidence", "end": END})
            graph.add_conditional_edges("evidence", route_after_evidence, {"compose": "compose", "end": END})
            graph.add_edge("compose", "final_validate")
            graph.add_edge("final_validate", END)
            compiled = graph.compile(checkpointer=None)  # no PHI at rest — see module docstring
        except Exception as exc:
            return runtime_unavailable_result(
                correlation_id=scope.correlation_id,
                patient_id=scope.patient_id,
                error_type=type(exc).__name__,
                elapsed_seconds=clock() - start,
            )

        # invoke() gets its OWN, separate net: this is where node functions
        # actually run and perform real reads, so a failure here must report
        # whatever specialists/reads `completed` already captured — reusing
        # runtime_unavailable_result()'s hardcoded-zero shape here would
        # silently understate completed chart/graph access in the audit
        # trail (a real review finding: this exact class of bug shipped
        # once already, when this net was wider and covered invoke() too).
        # An authorized request must still not crash either way, matching
        # runtimes/custom.py's identical contract.
        try:
            final_state = compiled.invoke({}, config={"recursion_limit": _RECURSION_LIMIT})
        except Exception as exc:
            chart = completed.get("chart")
            graph_result = completed.get("graph")
            # `failed_dispatch`: true only while an allowlisted specialist's
            # OWN node (chart/graph/evidence, beyond their two specifically-
            # handled exception types) is what raised — false once all
            # three have already appended themselves to `specialists_run`
            # and the failure is therefore in compose_node/final_validate_node
            # instead, which were never counted as a tool_call on the
            # successful path either (a real review finding: reusing the
            # dispatch-failure accounting here fabricated a phantom 4th
            # tool call for compose_node/final_validate_node failures).
            failed_dispatch = len(specialists_run) < 3
            return node_failure_result(
                correlation_id=scope.correlation_id,
                patient_id=scope.patient_id,
                specialists_run=specialists_run,
                chart_reads=chart.reads if chart else 0,
                graph_reads=graph_result.reads if graph_result else 0,
                chart_truncated=chart.truncated if chart else False,
                graph_truncated=graph_result.truncated if graph_result else False,
                error_type=type(exc).__name__,
                elapsed_seconds=clock() - start,
                failed_dispatch=failed_dispatch,
            )

        chart = final_state["chart"]

        if final_state.get("refused"):
            # `graph` is only in state if graph_node itself succeeded — a
            # refusal routed straight from graph_node (CrossPatientEvidenceError)
            # never set it, but DOES stash the read it already performed in
            # state["graph_reads"]/state["graph_truncated"] (see graph_node)
            # rather than letting these silently default to 0/False.
            graph_result = final_state.get("graph")
            graph_reads = graph_result.reads if graph_result else final_state.get("graph_reads", 0)
            graph_truncated = graph_result.truncated if graph_result else final_state.get("graph_truncated", False)
            return refused_result(
                correlation_id=scope.correlation_id,
                patient_id=scope.patient_id,
                specialists_run=specialists_run,
                reasons=final_state["refuse_reasons"],
                chart_reads=chart.reads,
                graph_reads=graph_reads,
                chart_truncated=chart.truncated,
                graph_truncated=graph_truncated,
                elapsed_seconds=clock() - start,
            )

        graph_result = final_state["graph"]

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
