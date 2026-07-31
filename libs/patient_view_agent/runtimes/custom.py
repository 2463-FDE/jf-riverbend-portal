"""Default PatientViewRuntime: the Week 4 fixed sequence — no framework.
This is the `custom` runtime selected by `build_runtime` and the only one
`run_patient_view()` ever uses.

Sequential, not graph-based: authorize -> chart specialist -> graph
specialist -> evidence validator -> composer -> shared final-validation
helper (`runtime.finalize_result`). Termination is structurally guaranteed
(no loop of any kind here; the composer's own bounded retry loop lives inside
`composer.compose()`), and every branch point matches
`runtimes/langgraph_runtime.py`'s graph exactly, since both call the same
`runtime.refused_result`/`runtime.finalize_result` helpers to turn validated
evidence + a composed summary into a `PatientViewResult`.

The graph specialist call is wrapped to catch `graph.CrossPatientEvidenceError`
— its own internal cross-patient tripwire, distinct from the evidence
validator's `EvidenceIntegrityError` — and degrade to a `refused_result()`
with `ViewReason.CROSS_PATIENT_EVIDENCE` rather than let it propagate,
per `PatientViewRuntime.run()`'s contract that nothing downstream of
authorization may raise. This mirrors `runtimes/langgraph_runtime.py`'s
identical handling in `graph_node`.

The chart->graph->evidence dispatch sequence and the compose->finalize
sequence each get their OWN outer `except Exception`, below the two
specific exception types above, both degrading to
`runtime.node_failure_result` for any OTHER unexpected failure (a
repository error, an unexpected composer/client exception, or any other
genuinely unforeseen bug) — the SAME contract
`runtimes/langgraph_runtime.py`'s `compiled.invoke()` catch-all already
enforces. `chart`/`graph` are read back from plain locals (this runtime is
sequential, not graph-based, so there's no `completed` closure dict to
mirror) so a failure after one or both specialists already completed still
reports the real reads that happened, never a fabricated zero.

The two nets are split, not one, because `node_failure_result`'s
`tool_calls` accounting differs by which net failed: Net 1 (dispatch) had
an allowlisted specialist call in flight that never got to append itself to
`specialists_run`, so `failed_dispatch=True` counts it. Net 2
(compose/finalize) runs only after all three specialists already
succeeded, so a failure there is never a specialist dispatch —
`failed_dispatch=False` reports `tool_calls=len(specialists_run)` (3, not a
fabricated 4), matching how compose was never counted as a tool_call on the
successful path either.
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
from ..runtime import PatientViewResult, finalize_result, initial_reasons, node_failure_result, refused_result
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


class CustomPatientViewRuntime:
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
        scope = authorizer.authorize(request)  # raises AuthorizationDenied; zero reads before this line

        specialists_run: list[str] = []
        # Populated as each specialist completes; read back by the outer
        # `except Exception` below for accurate partial-read accounting if
        # something later (a repo error, an unexpected composer/client
        # exception) fails — mirrors runtimes/langgraph_runtime.py's
        # `completed` closure dict, just as plain locals since this runtime
        # is sequential, not graph-based.
        chart = None
        graph = None

        # Net 1: chart/graph/evidence specialist dispatch. Anything that
        # reaches the outer `except Exception` here happened WHILE an
        # allowlisted specialist dispatch was in flight and never got to
        # append itself to specialists_run — `failed_dispatch=True` below
        # counts that attempt in tool_calls, matching refused_result()'s
        # identical accounting for the two specific exception types.
        try:
            chart = run_specialist(
                CHART_READ_TOOL, repository.load_chart, scope.patient_id, correlation_id=scope.correlation_id
            )
            specialists_run.append(CHART_READ_TOOL)

            try:
                graph = run_specialist(GRAPH_READ_TOOL, PatientGraphReader(scope, repository, limits=limits).build)
            except CrossPatientEvidenceError as exc:
                # The graph specialist's OWN internal cross-patient tripwire
                # (graph.py's `_reject`) fired — this must degrade to a safe
                # refusal like any other evidence-integrity failure, per
                # PatientViewRuntime.run()'s contract that nothing downstream
                # of authorization may raise. Not appended to
                # specialists_run: the call was rejected, not completed.
                # `exc.reads`/`exc.truncated` carry the repository read that
                # already happened inside `PatientGraphReader.build()`
                # before it rejected, so this refusal doesn't understate
                # access in the audit trail.
                return refused_result(
                    correlation_id=scope.correlation_id,
                    patient_id=scope.patient_id,
                    specialists_run=specialists_run,
                    reasons=[ViewReason.CROSS_PATIENT_EVIDENCE],
                    chart_reads=chart.reads,
                    graph_reads=exc.reads,
                    chart_truncated=chart.truncated,
                    graph_truncated=exc.truncated,
                    elapsed_seconds=clock() - start,
                )
            specialists_run.append(GRAPH_READ_TOOL)

            try:
                evidence = run_specialist(EVIDENCE_VALIDATE_TOOL, validate_evidence, scope, chart, graph)
            except EvidenceIntegrityError as exc:
                return refused_result(
                    correlation_id=scope.correlation_id,
                    patient_id=scope.patient_id,
                    specialists_run=specialists_run,
                    reasons=exc.reasons,
                    chart_reads=chart.reads,
                    graph_reads=graph.reads,
                    chart_truncated=chart.truncated,
                    graph_truncated=graph.truncated,
                    elapsed_seconds=clock() - start,
                )
            specialists_run.append(EVIDENCE_VALIDATE_TOOL)
        except Exception as exc:
            # Catches anything NOT already specifically classified above
            # (CrossPatientEvidenceError/EvidenceIntegrityError both `return`
            # before reaching here): a repository error, or any other
            # genuinely unforeseen bug during chart/graph/evidence dispatch.
            # `chart`/`graph` are read back from the locals above —
            # whichever specialists actually completed before the failure —
            # so this reports real partial reads, never a fabricated zero.
            return node_failure_result(
                correlation_id=scope.correlation_id,
                patient_id=scope.patient_id,
                specialists_run=specialists_run,
                chart_reads=chart.reads if chart else 0,
                graph_reads=graph.reads if graph else 0,
                chart_truncated=chart.truncated if chart else False,
                graph_truncated=graph.truncated if graph else False,
                error_type=type(exc).__name__,
                elapsed_seconds=clock() - start,
                failed_dispatch=True,
            )

        # Net 2: compose + finalize, AFTER all three specialists already
        # succeeded. A failure here is NOT a specialist dispatch — composing
        # was never counted as a tool_call on the successful path either
        # (finalize_result()'s tool_calls=len(specialists_run), no +1) — so
        # `failed_dispatch=False` here, unlike Net 1. This is the specific
        # gap a review caught: reusing Net 1's accounting for this net would
        # report a phantom 4th tool call (len(specialists_run)+1) when only
        # 3 dispatches ever ran.
        reasons: list[ViewReason] = initial_reasons(request, evidence)
        try:
            composed, compose_attempts, used_fallback = compose_fn(scope, evidence, llm_client=llm_client)
            if used_fallback:
                reasons.append(ViewReason.COMPOSE_FELL_BACK)

            return finalize_result(
                correlation_id=scope.correlation_id,
                patient_id=scope.patient_id,
                specialists_run=specialists_run,
                evidence=evidence,
                composed=composed,
                compose_attempts=compose_attempts,
                reasons=reasons,
                chart_reads=chart.reads,
                graph_reads=graph.reads,
                chart_truncated=chart.truncated,
                graph_truncated=graph.truncated,
                elapsed_seconds=clock() - start,
                max_seconds=max_seconds,
            )
        except Exception as exc:
            # compose_fn is an arbitrary injectable callable; if it raises
            # instead of returning its (composed, attempts, used_fallback)
            # tuple, there is no channel to recover how many attempts it
            # made — compose_attempts stays at node_failure_result()'s
            # default of 0 (unknown, not a claim that zero attempts ran).
            return node_failure_result(
                correlation_id=scope.correlation_id,
                patient_id=scope.patient_id,
                specialists_run=specialists_run,
                chart_reads=chart.reads,
                graph_reads=graph.reads,
                chart_truncated=chart.truncated,
                graph_truncated=graph.truncated,
                error_type=type(exc).__name__,
                elapsed_seconds=clock() - start,
                failed_dispatch=False,
            )
