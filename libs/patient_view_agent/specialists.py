"""Stage 3 fixed, read-only specialists.

Two independent read specialists (chart, graph) are bound to the SAME
`AuthorizedScope` produced by Stage 2's `AuthorizationPort.authorize()`, then
cross-checked against each other by a third specialist (the evidence
validator) before any composition happens. This is the same defense-in-depth
idea Stage 2 already applies inside a single graph build (re-checking every
row/node against `scope.patient_id`), extended one level up: two
independently-obtained reads must agree, not just each individually look
internally consistent.

Every specialist here is dispatched through `run_specialist()`, the single
allow-listed dispatch point. There is no model anywhere in this module and no
model-supplied tool name, patient id, or argument reaches it — every call
site in `runtime.py` passes a hardcoded literal from `ALLOWED_TOOLS`. The
allowlist check exists as a structural tripwire against a future coding
mistake (e.g. a copy-pasted call site naming the wrong tool), not because a
caller can currently choose the tool dynamically.
"""
from __future__ import annotations

from enum import Enum
from typing import Callable

from pydantic import BaseModel, ConfigDict

from libs.safe_logging import get_safe_logger

from .contracts import AuthorizedScope, ChartResult, NodeType, PatientGraph

log = get_safe_logger(__name__)

CHART_READ_TOOL = "chart_read"
GRAPH_READ_TOOL = "graph_read"
EVIDENCE_VALIDATE_TOOL = "evidence_validate"
ALLOWED_TOOLS = frozenset({CHART_READ_TOOL, GRAPH_READ_TOOL, EVIDENCE_VALIDATE_TOOL})


class SpecialistError(Exception):
    """Raised when dispatch names a tool outside the fixed allowlist. Carries
    no patient id, actor id, or free text — just the fact that dispatch was
    rejected before `fn` was ever called."""


def run_specialist(tool_name: str, fn: Callable, *args, **kwargs):
    """The only way a specialist's underlying read is invoked. Rejects any
    `tool_name` outside `ALLOWED_TOOLS` before `fn` runs at all."""
    if tool_name not in ALLOWED_TOOLS:
        log.warning("patient_view specialist dispatch rejected (reason=unknown_tool)")
        raise SpecialistError("unknown tool rejected")
    return fn(*args, **kwargs)


class ViewReason(str, Enum):
    """Coarse, PHI-free reasons the supervisor cites in its final result."""

    EVIDENCE_FOUND = "evidence_found"
    NO_EVIDENCE = "no_evidence"
    CROSS_PATIENT_EVIDENCE = "cross_patient_evidence"
    UNSUPPORTED_EVIDENCE = "unsupported_evidence"
    NON_TREATMENT_PURPOSE = "non_treatment_purpose"
    COMPOSE_FELL_BACK = "compose_fell_back"
    TIMEOUT = "timeout"


class EvidenceIntegrityError(Exception):
    """Raised when the chart specialist and graph specialist — two
    INDEPENDENT reads bound to the same scope — disagree in a way that means
    at least one cannot be trusted (cross-patient leakage, or a graph node
    unsupported by the chart read). Carries only the correlation id and a
    coarse reason list — never a patient id, actor id, or row content."""

    def __init__(self, correlation_id: str, reasons: list):
        self.correlation_id = correlation_id
        self.reasons = reasons
        super().__init__("evidence integrity check failed")


class ValidatedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chart: ChartResult
    graph: PatientGraph
    no_evidence: bool
    limitations: list[str]
    evidence_ids: list[str]


_LIMITATION_PROVIDER_PROJECTION = (
    "Provider nodes are projected from the free-text encounters.provider column, "
    "not a verified provider identity or FK (docs/planning/W4-patient-knowledge-graph.md)."
)
_LIMITATION_IDENTITY_FRAGMENTATION = (
    "This view reflects exactly one patient_id. Duplicate-person records are not "
    "reconciled (no MPI/match key exists upstream) — the same real person may have "
    "another, unrelated patient_id in this system."
)
_LIMITATION_TRUNCATED = "One or more configured row/node limits were reached; this view may be incomplete."


def validate_evidence(scope: AuthorizedScope, chart: ChartResult, graph: PatientGraph) -> ValidatedEvidence:
    """The evidence-validator specialist.

    Cross-checks the chart specialist's read and the graph specialist's read
    — two independent calls bound to the same scope — against each other and
    against `scope.patient_id`, before any composition happens. Fails closed:
    raises `EvidenceIntegrityError` rather than silently preferring one read
    over the other or dropping the disagreement.
    """
    cid = scope.correlation_id
    pid = scope.patient_id

    if chart.patient_id != pid or graph.patient_id != pid:
        raise EvidenceIntegrityError(cid, [ViewReason.CROSS_PATIENT_EVIDENCE])
    if any(e.patient_id != pid for e in chart.encounters) or any(r.patient_id != pid for r in chart.records):
        raise EvidenceIntegrityError(cid, [ViewReason.CROSS_PATIENT_EVIDENCE])
    if any(n.patient_id != pid for n in graph.nodes):
        raise EvidenceIntegrityError(cid, [ViewReason.CROSS_PATIENT_EVIDENCE])

    chart_record_ids = {str(r.id) for r in chart.records}
    chart_encounter_ids = {str(e.id) for e in chart.encounters}
    for node in graph.nodes:
        suffix = node.node_id.split(":", 1)[1]
        if node.node_type == NodeType.RECORD and suffix not in chart_record_ids:
            raise EvidenceIntegrityError(cid, [ViewReason.UNSUPPORTED_EVIDENCE])
        if node.node_type == NodeType.ENCOUNTER and suffix not in chart_encounter_ids:
            raise EvidenceIntegrityError(cid, [ViewReason.UNSUPPORTED_EVIDENCE])

    no_evidence = not chart.encounters and not chart.records

    limitations = [_LIMITATION_PROVIDER_PROJECTION, _LIMITATION_IDENTITY_FRAGMENTATION]
    if chart.truncated or graph.truncated:
        limitations.append(_LIMITATION_TRUNCATED)

    log.info(
        "patient_view evidence validated (correlation_id=%s, no_evidence=%s, evidence_count=%s)",
        cid,
        no_evidence,
        len(graph.evidence_ids),
    )
    return ValidatedEvidence(
        chart=chart,
        graph=graph,
        no_evidence=no_evidence,
        limitations=limitations,
        evidence_ids=list(graph.evidence_ids),
    )
