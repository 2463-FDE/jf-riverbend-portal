"""Stage 3 — the bounded, deterministic patient-view supervisor.

Fixed sequence, no peer delegation, no model-supplied patient id, tool name,
URL, or SQL anywhere in this module:

    authorize -> chart specialist + graph specialist -> evidence validator
    -> composer -> final validator

`run_patient_view()` is the single public entrypoint, unchanged from Week 4:
it takes a code-supplied `AuthorizationRequest` and an `AuthorizationPort` +
`ChartRepositoryPort` — exactly Stage 2's `build_patient_graph()` shape — and
extends that same authorize-then-read invariant with the fixed specialist
fan-out. Authorization happens exactly once, before either read specialist
runs; a denial raises `AuthorizationDenied` here, so the repository and graph
reader are never constructed and zero reads occur (same guarantee
`build_patient_graph` already provides, re-verified by
`tests/test_patient_view_runtime.py`).

Week 5 adds a `PatientViewRuntime` protocol and a fail-closed `build_runtime`
factory (mirroring `libs/eligibility_agent/runtime.py::build_agent_runtime`):
the sequence above is now the `custom` runtime
(`runtimes/custom.py::CustomPatientViewRuntime`), the default and rollback.
An optional `langgraph` runtime (`runtimes/langgraph_runtime.py`) implements
the identical sequence as a `StateGraph` for comparison — see
`docs/analysis/W5-orchestration-framework-evaluation.md` §8. `run_patient_view`
itself is unchanged: it always builds and runs the `custom` runtime, so every
existing caller (including `demo.py`) behaves exactly as before.

This is a fixed state machine, not an autonomous agent deciding what to do
next, regardless of which runtime executes it. The only place a model can run
at all is `composer.compose()` (optional, off by default) — its only
affordance is phrasing already validator-approved evidence; it cannot choose
the patient, the tool, or which rows are evidence. A `final validator` step
immediately downstream of the composer re-checks its citations against the
evidence validator's approved set regardless — every specialist's output is
checked at least once after it runs, including the composer's. Both runtimes
call the SAME `_refused_result`/`_finalize_result` helpers below to turn
validated evidence + a composed summary into a `PatientViewResult` — not
independently-derived equivalent logic, but literally the same code, so the
equivalence the contract suite checks for is structural, not coincidental.

Escalation is decided entirely by this module's own code, never by the
composer/model: any purpose other than `TREATMENT`, any request with no
underlying evidence, an elapsed-time budget overrun, or a composer fallback
all force `escalation=True`. An evidence-integrity failure (cross-patient
leakage, or a composer citation outside the approved set) is refused
outright — no chart content is ever shown in that case, escalation or not.
"""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict

from libs.llm_client.client import LLMClient
from libs.safe_logging import get_safe_logger

from .authorization import AuthorizationPort
from .composer import compose as _default_compose
from .contracts import AuthorizationRequest, GraphLimits, Purpose
from .repository import ChartRepositoryPort
from .specialists import ValidatedEvidence, ViewReason

log = get_safe_logger(__name__)

_KNOWN_RUNTIMES = ("custom", "langgraph")

_AUTO_COMPLETE_PURPOSES = frozenset({Purpose.TREATMENT})
_ESCALATE_REASONS = frozenset(
    {
        ViewReason.NO_EVIDENCE,
        ViewReason.NON_TREATMENT_PURPOSE,
        ViewReason.TIMEOUT,
        ViewReason.COMPOSE_FELL_BACK,
        ViewReason.RUNTIME_UNAVAILABLE,
    }
)
_REFUSE_REASONS = frozenset({ViewReason.CROSS_PATIENT_EVIDENCE, ViewReason.UNSUPPORTED_EVIDENCE})

_SAFE_REFUSAL_SUMMARY = "This request could not be completed safely and has been refused. No chart content is shown."
_SAFE_RUNTIME_UNAVAILABLE_SUMMARY = (
    "This view requires human review before use: the selected orchestration runtime is unavailable right now."
)
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


class PatientViewRuntime(ABC):
    @abstractmethod
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
        """Run the fixed authorize -> chart + graph -> evidence -> compose ->
        final-validate sequence and return exactly one PatientViewResult.

        Must raise AuthorizationDenied (never return a partial result) on a
        DENY decision, before either read specialist runs. Must never raise
        for anything downstream of authorization — evidence-integrity
        failures and composer errors degrade to a REFUSED/ESCALATED result
        instead.
        """
        raise NotImplementedError


def build_runtime(name: str = None, **kwargs) -> PatientViewRuntime:
    """Fail closed: an unset/unrecognized PATIENT_VIEW_RUNTIME raises rather
    than silently falling back to any default, mirroring
    libs/eligibility_agent/runtime.py::build_agent_runtime. `custom` must be
    requested explicitly (by name or via the env var) — it is the configured
    default, not an implicit fallback for an unrecognized value.
    """
    name = name or os.getenv("PATIENT_VIEW_RUNTIME", "custom")

    if name == "custom":
        from .runtimes.custom import CustomPatientViewRuntime

        return CustomPatientViewRuntime(**kwargs)

    if name == "langgraph":
        from .runtimes.langgraph_runtime import LangGraphPatientViewRuntime

        return LangGraphPatientViewRuntime(**kwargs)

    raise ValueError(f"Unknown PATIENT_VIEW_RUNTIME '{name}' — expected one of: {', '.join(_KNOWN_RUNTIMES)}")


def initial_reasons(request: AuthorizationRequest, evidence: ValidatedEvidence) -> list[ViewReason]:
    """The reason list every runtime starts from once evidence validation has
    succeeded — shared so both runtimes derive it identically."""
    reasons: list[ViewReason] = [ViewReason.NO_EVIDENCE if evidence.no_evidence else ViewReason.EVIDENCE_FOUND]
    if request.purpose not in _AUTO_COMPLETE_PURPOSES:
        reasons.append(ViewReason.NON_TREATMENT_PURPOSE)
    return reasons


def refused_result(
    *,
    correlation_id: str,
    patient_id: int,
    specialists_run: list[str],
    reasons: list[ViewReason],
    chart_reads: int,
    graph_reads: int,
    chart_truncated: bool,
    graph_truncated: bool,
    elapsed_seconds: float,
) -> PatientViewResult:
    """Shared by both runtimes for the evidence-integrity-failure path: an
    independent-read disagreement is refused before composition ever runs.
    `tool_calls` counts the rejected evidence_validate dispatch even though it
    is deliberately not appended to `specialists_run` (it never completed)."""
    log.warning("patient_view refused (correlation_id=%s, reasons=%s)", correlation_id, [r.value for r in reasons])
    return PatientViewResult(
        outcome=PatientViewOutcome.REFUSED,
        summary=_SAFE_REFUSAL_SUMMARY,
        evidence_ids=[],
        limitations=[],
        escalation=True,
        reasons=reasons,
        correlation_id=correlation_id,
        patient_id=patient_id,
        execution=ExecutionMetadata(
            specialists_run=specialists_run,
            tool_calls=len(specialists_run) + 1,
            reads=chart_reads + graph_reads,
            truncated=chart_truncated or graph_truncated,
            compose_attempts=0,
            elapsed_seconds=elapsed_seconds,
        ),
    )


def runtime_unavailable_result(
    *,
    correlation_id: str,
    patient_id: int,
    error_type: str,
    elapsed_seconds: float,
) -> PatientViewResult:
    """Shared by any runtime whose OWN optional dependency turns out to be
    missing at request time (e.g. `runtimes/langgraph_runtime.py` when
    `langgraph` isn't installed). Selecting such a runtime is never fatal at
    `build_runtime()` time — the dependency is only actually needed once a
    request tries to run — so this degrades to a safe ESCALATED result
    instead of raising, satisfying `PatientViewRuntime.run()`'s contract that
    nothing downstream of authorization may raise. Zero reads have occurred
    by construction: this only ever fires before any specialist runs.
    `error_type` is the exception's TYPE only (e.g. "ModuleNotFoundError"),
    never a message — this can otherwise be the one place free-text from an
    external library's exception could leak into a PHI-safe log."""
    log.error(
        "patient_view runtime unavailable (correlation_id=%s, error_type=%s)", correlation_id, error_type
    )
    return PatientViewResult(
        outcome=PatientViewOutcome.ESCALATED,
        summary=_SAFE_RUNTIME_UNAVAILABLE_SUMMARY,
        evidence_ids=[],
        limitations=[],
        escalation=True,
        reasons=[ViewReason.RUNTIME_UNAVAILABLE],
        correlation_id=correlation_id,
        patient_id=patient_id,
        execution=ExecutionMetadata(
            specialists_run=[],
            tool_calls=0,
            reads=0,
            truncated=False,
            compose_attempts=0,
            elapsed_seconds=elapsed_seconds,
        ),
    )


def finalize_result(
    *,
    correlation_id: str,
    patient_id: int,
    specialists_run: list[str],
    evidence: ValidatedEvidence,
    composed,
    compose_attempts: int,
    reasons: list[ViewReason],
    chart_reads: int,
    graph_reads: int,
    chart_truncated: bool,
    graph_truncated: bool,
    elapsed_seconds: float,
    max_seconds: float,
) -> PatientViewResult:
    """Shared by both runtimes for the post-composition path: never trust the
    composer's citations on their own — re-check against the evidence
    validator's approved set (the final validator), then dispatch to
    REFUSED/ESCALATED/COMPLETED by this module's own deterministic rules,
    never the composer/model."""
    approved_ids = set(evidence.evidence_ids)
    if not set(composed.cited_evidence_ids) <= approved_ids:
        reasons = [*reasons, ViewReason.UNSUPPORTED_EVIDENCE]
    final_ids = [e for e in composed.cited_evidence_ids if e in approved_ids]

    if elapsed_seconds > max_seconds:
        reasons = [*reasons, ViewReason.TIMEOUT]

    execution = ExecutionMetadata(
        specialists_run=specialists_run,
        tool_calls=len(specialists_run),
        reads=chart_reads + graph_reads,
        truncated=chart_truncated or graph_truncated,
        compose_attempts=compose_attempts,
        elapsed_seconds=elapsed_seconds,
    )

    if _REFUSE_REASONS & set(reasons):
        log.warning("patient_view refused (correlation_id=%s, reasons=%s)", correlation_id, [r.value for r in reasons])
        return PatientViewResult(
            outcome=PatientViewOutcome.REFUSED,
            summary=_SAFE_REFUSAL_SUMMARY,
            evidence_ids=[],
            limitations=evidence.limitations,
            escalation=True,
            reasons=reasons,
            correlation_id=correlation_id,
            patient_id=patient_id,
            execution=execution,
        )

    if _ESCALATE_REASONS & set(reasons):
        log.info("patient_view escalated (correlation_id=%s, reasons=%s)", correlation_id, [r.value for r in reasons])
        return PatientViewResult(
            outcome=PatientViewOutcome.ESCALATED,
            summary=_SAFE_ESCALATION_PREFIX + composed.summary,
            evidence_ids=final_ids,
            limitations=evidence.limitations,
            escalation=True,
            reasons=reasons,
            correlation_id=correlation_id,
            patient_id=patient_id,
            execution=execution,
        )

    log.info("patient_view completed (correlation_id=%s, evidence_count=%s)", correlation_id, len(final_ids))
    return PatientViewResult(
        outcome=PatientViewOutcome.COMPLETED,
        summary=composed.summary,
        evidence_ids=final_ids,
        limitations=evidence.limitations,
        escalation=False,
        reasons=reasons,
        correlation_id=correlation_id,
        patient_id=patient_id,
        execution=execution,
    )


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
    """Unchanged public entrypoint: always runs the `custom` runtime — the
    default and rollback (see build_runtime). Selecting `langgraph` requires
    calling `build_runtime("langgraph").run(...)` explicitly; this function
    never reads PATIENT_VIEW_RUNTIME, so no env var can change what a caller
    of `run_patient_view` gets."""
    from .runtimes.custom import CustomPatientViewRuntime

    return CustomPatientViewRuntime().run(
        request,
        authorizer=authorizer,
        repository=repository,
        limits=limits,
        llm_client=llm_client,
        compose_fn=compose_fn,
        max_seconds=max_seconds,
        clock=clock,
    )
