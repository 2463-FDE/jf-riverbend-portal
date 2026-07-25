"""Stage 3 — the bounded, deterministic patient-view supervisor.

Fixed sequence, no peer delegation, no model-supplied patient id, tool name,
URL, or SQL anywhere in this module:

    authorize -> chart specialist + graph specialist -> evidence validator
    -> composer -> final validator

`run_patient_view()` is the single public entrypoint. It takes a
code-supplied `AuthorizationRequest` and an `AuthorizationPort` +
`ChartRepositoryPort` — exactly Stage 2's `build_patient_graph()` shape —
and extends that same authorize-then-read invariant with the fixed
specialist fan-out. Authorization happens exactly once, before either read
specialist runs; a denial raises `AuthorizationDenied` here, so the
repository and graph reader are never constructed and zero reads occur
(same guarantee `build_patient_graph` already provides, re-verified by
`tests/test_patient_view_runtime.py`).

This is a fixed state machine, not an autonomous agent deciding what to do
next. The only place a model can run at all is `composer.compose()`
(optional, off by default) — its only affordance is phrasing already
validator-approved evidence; it cannot choose the patient, the tool, or
which rows are evidence. A `final validator` step immediately downstream of
the composer re-checks its citations against the evidence validator's
approved set regardless — every specialist's output is checked at least
once after it runs, including the composer's.

Escalation is decided entirely by this module's own code, never by the
composer/model: any purpose other than `TREATMENT`, any request with no
underlying evidence, an elapsed-time budget overrun, or a composer fallback
all force `escalation=True`. An evidence-integrity failure (cross-patient
leakage, or a composer citation outside the approved set) is refused
outright — no chart content is ever shown in that case, escalation or not.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict

from libs.llm_client.client import LLMClient
from libs.safe_logging import get_safe_logger

from .authorization import AuthorizationPort
from .composer import compose as _default_compose
from .contracts import AuthorizationRequest, GraphLimits, Purpose
from .graph import PatientGraphReader
from .repository import ChartRepositoryPort
from .specialists import (
    CHART_READ_TOOL,
    EVIDENCE_VALIDATE_TOOL,
    GRAPH_READ_TOOL,
    EvidenceIntegrityError,
    ViewReason,
    run_specialist,
    validate_evidence,
)

log = get_safe_logger(__name__)

_AUTO_COMPLETE_PURPOSES = frozenset({Purpose.TREATMENT})
_ESCALATE_REASONS = frozenset(
    {ViewReason.NO_EVIDENCE, ViewReason.NON_TREATMENT_PURPOSE, ViewReason.TIMEOUT, ViewReason.COMPOSE_FELL_BACK}
)
_REFUSE_REASONS = frozenset({ViewReason.CROSS_PATIENT_EVIDENCE, ViewReason.UNSUPPORTED_EVIDENCE})

_SAFE_REFUSAL_SUMMARY = "This request could not be completed safely and has been refused. No chart content is shown."
_SAFE_ESCALATION_PREFIX = "This view requires human review before use: "


class PatientViewOutcome(str, Enum):
    COMPLETED = "completed"
    ESCALATED = "escalated"
    REFUSED = "refused"


class ExecutionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    specialists_run: list[str]
    tool_calls: int
    reads: int
    truncated: bool
    compose_attempts: int
    elapsed_seconds: float


class PatientViewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: PatientViewOutcome
    summary: str
    evidence_ids: list[str]
    limitations: list[str]
    escalation: bool
    reasons: list[ViewReason]
    correlation_id: str
    patient_id: int
    execution: ExecutionMetadata


def run_patient_view(
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

    chart = run_specialist(
        CHART_READ_TOOL, repository.load_chart, scope.patient_id, correlation_id=scope.correlation_id
    )
    specialists_run.append(CHART_READ_TOOL)

    graph = run_specialist(GRAPH_READ_TOOL, PatientGraphReader(scope, repository, limits=limits).build)
    specialists_run.append(GRAPH_READ_TOOL)

    try:
        evidence = run_specialist(EVIDENCE_VALIDATE_TOOL, validate_evidence, scope, chart, graph)
    except EvidenceIntegrityError as exc:
        log.warning(
            "patient_view refused (correlation_id=%s, reasons=%s)",
            exc.correlation_id,
            [r.value for r in exc.reasons],
        )
        return PatientViewResult(
            outcome=PatientViewOutcome.REFUSED,
            summary=_SAFE_REFUSAL_SUMMARY,
            evidence_ids=[],
            limitations=[],
            escalation=True,
            reasons=exc.reasons,
            correlation_id=scope.correlation_id,
            patient_id=scope.patient_id,
            execution=ExecutionMetadata(
                specialists_run=specialists_run,
                tool_calls=len(specialists_run) + 1,  # the rejected evidence_validate call counts too
                reads=chart.reads + graph.reads,
                truncated=chart.truncated or graph.truncated,
                compose_attempts=0,
                elapsed_seconds=clock() - start,
            ),
        )
    specialists_run.append(EVIDENCE_VALIDATE_TOOL)

    reasons: list[ViewReason] = [ViewReason.NO_EVIDENCE if evidence.no_evidence else ViewReason.EVIDENCE_FOUND]
    if request.purpose not in _AUTO_COMPLETE_PURPOSES:
        reasons.append(ViewReason.NON_TREATMENT_PURPOSE)

    composed, compose_attempts, used_fallback = compose_fn(scope, evidence, llm_client=llm_client)
    if used_fallback:
        reasons.append(ViewReason.COMPOSE_FELL_BACK)

    # Final validator: never trust the composer's citations on their own —
    # re-check against the evidence validator's approved set, the same way
    # every prior specialist's output was already re-checked upstream.
    approved_ids = set(evidence.evidence_ids)
    if not set(composed.cited_evidence_ids) <= approved_ids:
        reasons.append(ViewReason.UNSUPPORTED_EVIDENCE)
    final_ids = [e for e in composed.cited_evidence_ids if e in approved_ids]

    elapsed = clock() - start
    if elapsed > max_seconds:
        reasons.append(ViewReason.TIMEOUT)

    execution = ExecutionMetadata(
        specialists_run=specialists_run,
        tool_calls=len(specialists_run),
        reads=chart.reads + graph.reads,
        truncated=chart.truncated or graph.truncated,
        compose_attempts=compose_attempts,
        elapsed_seconds=elapsed,
    )

    if _REFUSE_REASONS & set(reasons):
        log.warning("patient_view refused (correlation_id=%s, reasons=%s)", scope.correlation_id, [r.value for r in reasons])
        return PatientViewResult(
            outcome=PatientViewOutcome.REFUSED,
            summary=_SAFE_REFUSAL_SUMMARY,
            evidence_ids=[],
            limitations=evidence.limitations,
            escalation=True,
            reasons=reasons,
            correlation_id=scope.correlation_id,
            patient_id=scope.patient_id,
            execution=execution,
        )

    if _ESCALATE_REASONS & set(reasons):
        log.info("patient_view escalated (correlation_id=%s, reasons=%s)", scope.correlation_id, [r.value for r in reasons])
        return PatientViewResult(
            outcome=PatientViewOutcome.ESCALATED,
            summary=_SAFE_ESCALATION_PREFIX + composed.summary,
            evidence_ids=final_ids,
            limitations=evidence.limitations,
            escalation=True,
            reasons=reasons,
            correlation_id=scope.correlation_id,
            patient_id=scope.patient_id,
            execution=execution,
        )

    log.info("patient_view completed (correlation_id=%s, evidence_count=%s)", scope.correlation_id, len(final_ids))
    return PatientViewResult(
        outcome=PatientViewOutcome.COMPLETED,
        summary=composed.summary,
        evidence_ids=final_ids,
        limitations=evidence.limitations,
        escalation=False,
        reasons=reasons,
        correlation_id=scope.correlation_id,
        patient_id=scope.patient_id,
        execution=execution,
    )
