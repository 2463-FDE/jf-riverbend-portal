"""Seeded in-memory patient graph reader — the boundary that turns an already
AUTHORIZED scope into a bounded `Patient -> Encounter -> Provider -> Record`
projection.

Key safety properties (docs/planning/W4-patient-knowledge-graph.md):

- Bound at construction. `PatientGraphReader(scope, repository)` takes its
  patient id from `scope.patient_id`; `build()` accepts no patient id. There is
  no code path that turns an arbitrary integer into a read — the scope came
  from an ALLOW decision, nothing else.
- Fail-closed cross-patient rejection. Every row and node is re-checked against
  `scope.patient_id`; any mismatch raises `CrossPatientEvidenceError` before it
  can appear in output. This treats a patient's demographics and chart as ONE
  protected scope (per docs/analysis/RIV-201-patient-records-IDOR.md): evidence
  is rejected whenever its `patient_id` differs from scope, regardless of which
  projection produced it.
- Bounded. Encounter/record counts are capped by the repository; node/edge
  counts are capped here. Truncation is deterministic and flagged.
- Provider nodes are PROJECTED from `encounters.provider` free text and are
  labelled as such (`projected=True`, `provenance=...`). No FK to the providers
  table is claimed, and no `Provider -> Record` authorship edge is created (the
  schema cannot prove it).
- Minimum-necessary. Nodes carry ids + light metadata (type/status/kind/title);
  record bodies are never loaded (see repository) and never appear in a node.
- PHI-safe logging: only correlation id + counts, never a patient id, name, or
  clinical content.
"""
from __future__ import annotations

import re

from libs.safe_logging import get_safe_logger

from .authorization import AuthorizationPort
from .contracts import (
    AuthorizationRequest,
    AuthorizedScope,
    Edge,
    EdgeType,
    GraphLimits,
    GraphNode,
    NodeType,
    PatientGraph,
)
from .repository import ChartRepositoryPort

log = get_safe_logger(__name__)

_TYPE_RANK = {
    NodeType.PATIENT: 0,
    NodeType.ENCOUNTER: 1,
    NodeType.PROVIDER: 2,
    NodeType.RECORD: 3,
}

_PROVIDER_PROVENANCE = (
    "projected from encounters.provider free text; not an FK to the providers "
    "table, and no Provider->Record authorship is asserted"
)


class CrossPatientEvidenceError(Exception):
    """Raised when a row/node's patient id differs from the bound scope. The
    message carries only the correlation id — never a patient id.

    Carries `reads`/`truncated` from the `ChartResult` that was already
    loaded by `load_chart()` before the mismatch was found — the repository
    read happened regardless of whether `_reject()` fires, so a caller
    turning this into a refusal (rather than letting it propagate) can
    still report accurate audit metadata instead of silently claiming zero
    reads for a specialist call that did perform one."""

    def __init__(self, correlation_id: str, *, reads: int = 0, truncated: bool = False):
        self.correlation_id = correlation_id
        self.reads = reads
        self.truncated = truncated
        super().__init__("cross-patient evidence rejected")


def _provider_slug(provider: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", provider.strip().lower()).strip("-")
    return slug or "unknown"


def _node_sort_key(node: GraphNode):
    suffix = node.node_id.split(":", 1)[1]
    if suffix.isdigit():
        return (_TYPE_RANK[node.node_type], 0, int(suffix), "")
    return (_TYPE_RANK[node.node_type], 1, 0, suffix)


class PatientGraphReader:
    def __init__(
        self,
        scope: AuthorizedScope,
        repository: ChartRepositoryPort,
        *,
        limits: GraphLimits | None = None,
    ):
        self._scope = scope
        self._repository = repository
        self._limits = limits or GraphLimits()

    def build(self) -> PatientGraph:
        pid = self._scope.patient_id
        cid = self._scope.correlation_id
        limits = self._limits

        chart = self._repository.load_chart(pid, correlation_id=cid)
        if chart.patient_id != pid:
            self._reject(cid, reads=chart.reads, truncated=chart.truncated)

        nodes: dict[str, GraphNode] = {}
        edges: list[Edge] = []
        dropped_dangling = 0
        truncated = chart.truncated

        def add_node(node: GraphNode) -> bool:
            """Add a node if capacity remains. Returns False (and flags
            truncation) when the node cap is hit, so no dangling edge is made."""
            nonlocal truncated
            if node.node_id in nodes:
                return True  # dedupe duplicate ids deterministically (keep first)
            if len(nodes) >= limits.max_nodes:
                truncated = True
                return False
            nodes[node.node_id] = node
            return True

        def add_edge(source_id: str, target_id: str, edge_type: EdgeType) -> None:
            nonlocal truncated
            if len(edges) >= limits.max_edges:
                truncated = True
                return
            edges.append(Edge(source_id=source_id, target_id=target_id, edge_type=edge_type))

        # Patient root.
        patient_node_id = f"patient:{pid}"
        add_node(GraphNode(node_id=patient_node_id, node_type=NodeType.PATIENT, patient_id=pid))

        encounter_ids: set[int] = set()
        for enc in chart.encounters:
            if enc.patient_id != pid:
                self._reject(cid, reads=chart.reads, truncated=truncated)  # defense in depth over the repository's own filter
            enc_node_id = f"encounter:{enc.id}"
            if not add_node(
                GraphNode(
                    node_id=enc_node_id,
                    node_type=NodeType.ENCOUNTER,
                    patient_id=pid,
                    attributes={"encounter_type": enc.encounter_type, "status": enc.status},
                )
            ):
                break
            encounter_ids.add(enc.id)
            add_edge(patient_node_id, enc_node_id, EdgeType.HAS_ENCOUNTER)

            if enc.provider:
                provider_node_id = f"provider:{_provider_slug(enc.provider)}"
                add_node(
                    GraphNode(
                        node_id=provider_node_id,
                        node_type=NodeType.PROVIDER,
                        patient_id=pid,
                        attributes={"display": enc.provider.strip()},
                        projected=True,
                        provenance=_PROVIDER_PROVENANCE,
                    )
                )
                # Only add the edge if the provider node actually exists (cap).
                if provider_node_id in nodes:
                    add_edge(enc_node_id, provider_node_id, EdgeType.SEEN_BY)

        for rec in chart.records:
            if rec.patient_id != pid:
                self._reject(cid, reads=chart.reads, truncated=truncated)
            if rec.encounter_id not in encounter_ids:
                # Record points at an encounter not in this patient's scope
                # (missing node). Drop it safely — never fabricate a phantom
                # encounter node to hang it from.
                dropped_dangling += 1
                continue
            rec_node_id = f"record:{rec.id}"
            if not add_node(
                GraphNode(
                    node_id=rec_node_id,
                    node_type=NodeType.RECORD,
                    patient_id=pid,
                    attributes={"kind": rec.kind, "title": rec.title, "status": rec.status},
                )
            ):
                break
            add_edge(f"encounter:{rec.encounter_id}", rec_node_id, EdgeType.HAS_RECORD)

        ordered_nodes = sorted(nodes.values(), key=_node_sort_key)
        ordered_edges = sorted(
            edges, key=lambda e: (e.edge_type.value, e.source_id, e.target_id)
        )
        evidence_ids = [n.node_id for n in ordered_nodes]

        log.info(
            "patient_view graph built (correlation_id=%s, nodes=%s, edges=%s, reads=%s, truncated=%s, dropped=%s)",
            cid,
            len(ordered_nodes),
            len(ordered_edges),
            chart.reads,
            truncated,
            dropped_dangling,
        )
        return PatientGraph(
            patient_id=pid,
            correlation_id=cid,
            nodes=ordered_nodes,
            edges=ordered_edges,
            evidence_ids=evidence_ids,
            reads=chart.reads,
            truncated=truncated,
            dropped_dangling=dropped_dangling,
        )

    def _reject(self, correlation_id: str, *, reads: int, truncated: bool) -> None:
        log.warning(
            "patient_view evidence rejected (reason=cross_patient, correlation_id=%s)",
            correlation_id,
        )
        raise CrossPatientEvidenceError(correlation_id, reads=reads, truncated=truncated)


def build_patient_graph(
    request: AuthorizationRequest,
    *,
    authorizer: AuthorizationPort,
    repository: ChartRepositoryPort,
    limits: GraphLimits | None = None,
) -> PatientGraph:
    """End-to-end Stage 2 flow: authorize FIRST, then read.

    If `authorizer.authorize()` denies, it raises `AuthorizationDenied` here and
    the repository/reader are never constructed — so a denied request performs
    zero reads. This is the seam Stage 3's supervisor will drive.
    """
    scope = authorizer.authorize(request)  # raises AuthorizationDenied on deny
    reader = PatientGraphReader(scope, repository, limits=limits)
    return reader.build()
