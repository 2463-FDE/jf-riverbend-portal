"""Week 4 patient-view graph core — a deterministic, fail-closed, read-only
boundary that turns an AUTHORIZED scope into a bounded patient knowledge graph.

Authorization happens first and owns access decisions (`authorization.py`);
retrieval is bound to the authorized patient at construction (`graph.py`,
`repository.py`); nothing here is a model responsibility. This is Stage 2 of
the Week 4 plan — the graph/authorization core. The bounded multi-agent
supervisor (Stage 3) is not part of this package yet.

This boundary is DEFENSE IN DEPTH for the new prototype code path only. It does
not touch, and does not remediate, the RIV-201 IDOR in
services/gateway/app.py or services/records-service/app.py.
"""
from .authorization import AuthorizationDenied, AuthorizationPort, FakePolicyAuthorization
from .contracts import (
    Action,
    AuthorizationRequest,
    AuthorizedScope,
    ChartResult,
    Denial,
    DenialReason,
    Edge,
    EdgeType,
    EncounterRow,
    GraphLimits,
    GraphNode,
    NodeType,
    PatientGraph,
    Purpose,
    RecordRow,
)
from .graph import CrossPatientEvidenceError, PatientGraphReader, build_patient_graph
from .repository import ChartRepositoryPort, SeededChartRepository, seed_derived_sample

__all__ = [
    # enums
    "Action",
    "Purpose",
    "NodeType",
    "EdgeType",
    "DenialReason",
    # contracts
    "AuthorizationRequest",
    "AuthorizedScope",
    "Denial",
    "GraphLimits",
    "EncounterRow",
    "RecordRow",
    "ChartResult",
    "GraphNode",
    "Edge",
    "PatientGraph",
    # authorization
    "AuthorizationPort",
    "FakePolicyAuthorization",
    "AuthorizationDenied",
    # repository
    "ChartRepositoryPort",
    "SeededChartRepository",
    "seed_derived_sample",
    # graph
    "PatientGraphReader",
    "CrossPatientEvidenceError",
    "build_patient_graph",
]
