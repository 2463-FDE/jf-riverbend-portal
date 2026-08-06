"""Week 4 patient-view graph core — a deterministic, fail-closed, read-only
boundary that turns an AUTHORIZED scope into a bounded patient knowledge graph,
plus (Stage 3) a bounded, fixed-sequence supervisor that assembles a cited
patient view on top of it.

Authorization happens first and owns access decisions (`authorization.py`);
retrieval is bound to the authorized patient at construction (`graph.py`,
`repository.py`); the supervisor (`runtime.py`) runs a fixed
authorize -> chart specialist + graph specialist -> evidence validator ->
composer -> final validator sequence with no peer delegation. Nothing here is
a model responsibility except optionally phrasing already-validated evidence
in `composer.py`.

This boundary is DEFENSE IN DEPTH. It does not touch, and does not remediate,
the RIV-201 IDOR in services/gateway/app.py or services/records-service/app.py
(`proxy_records`/`proxy_patient`/`get_patient_records`/`get_patient` remain
exactly as exploitable as documented). A later stage does wire this
supervisor into one new, additive production route
(`GET /patients/{id}/view`, gateway -> records-service), gated by
`StaffAccessGate` — a real, fail-closed but explicitly NOT patient-specific
authorization check (see `staff_access_gate.py`) — with a real database
repository (`services/records-service/patient_view_repository.py`) in place
of `SeededChartRepository`. That route is additive; the legacy IDOR-exposed
endpoints above are untouched by it.
"""
from .authorization import AuthorizationDenied, AuthorizationPort, FakePolicyAuthorization
from .staff_access_gate import StaffAccessGate
from .contracts import (
    Action,
    AuthorizationRequest,
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
from .graph import CrossPatientEvidenceError, build_patient_graph
from .repository import ChartRepositoryPort, SeededChartRepository, seed_derived_sample
from .runtime import (
    ExecutionMetadata,
    PatientViewOutcome,
    PatientViewResult,
    PatientViewRuntime,
    build_runtime,
    run_patient_view,
)
from .specialists import EvidenceIntegrityError, SpecialistError, ViewReason

# `build_patient_graph()` is the single public retrieval entrypoint: it takes an
# AuthorizationRequest + an AuthorizationPort and authorizes immediately before
# any repository access. `AuthorizedScope` (forgery-guarded) and
# `PatientGraphReader` are deliberately NOT exported — they are internal
# building blocks, reachable via submodules only for white-box tests, so no
# caller is handed an API that accepts a self-minted scope.
__all__ = [
    # enums
    "Action",
    "Purpose",
    "NodeType",
    "EdgeType",
    "DenialReason",
    # contracts
    "AuthorizationRequest",
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
    "StaffAccessGate",
    # repository
    "ChartRepositoryPort",
    "SeededChartRepository",
    "seed_derived_sample",
    # graph — single public entrypoint + the fail-closed error type
    "CrossPatientEvidenceError",
    "build_patient_graph",
    # Stage 3 — bounded supervisor
    "ViewReason",
    "SpecialistError",
    "EvidenceIntegrityError",
    "PatientViewOutcome",
    "ExecutionMetadata",
    "PatientViewResult",
    "run_patient_view",
    # Week 5 — swappable runtime contract (custom is the default and rollback)
    "PatientViewRuntime",
    "build_runtime",
]
