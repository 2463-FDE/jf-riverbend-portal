"""Framework-neutral typed contracts for the Week 4 patient-view graph core.

Design invariants encoded here (see docs/planning/W4-patient-knowledge-graph.md
and docs/analysis/RIV-201-patient-records-IDOR.md):

- `AuthorizedScope` is the ONLY object that unlocks retrieval. It is produced
  solely by an `AuthorizationPort.authorize()` ALLOW decision. A graph reader
  and a repository are bound to `AuthorizedScope.patient_id` at construction —
  there is deliberately no other way to name which patient to read, so a model
  (Stage 3) can never turn an arbitrary integer into a retrieval request.
- Nodes carry evidence *handles* (`node_id`) plus minimum-necessary metadata.
  Record BODIES are never modelled here and never reach a node — the read model
  does not even load them (`RecordRow` has no `body` field), which is stricter
  than the existing `/patients/{id}/records` endpoint that returns full bodies.
- Provider nodes are a DOCUMENTED PROJECTION from `encounters.provider` free
  text. They are not a foreign key to the scheduling `providers` table, and
  `Provider -> Record` authorship cannot be proven from the schema. Every
  Provider node therefore carries `projected=True` and a `provenance` string.
- Denials carry no PHI: a `Denial` has a coarse reason enum + correlation id,
  never actor id, patient id, name, or free text.

No eligibility-specific classes are imported; this package reuses the Week 3
*patterns* (bounded, strict, fail-closed, PHI-safe logging), not its code.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Action(str, Enum):
    VIEW_PATIENT_CHART = "view_patient_chart"


class Purpose(str, Enum):
    """Coarse HIPAA-flavoured access purpose. Deliberately small; a real policy
    engine (Week 9) would model this against 45 CFR 164.502(b)."""

    TREATMENT = "treatment"
    PAYMENT = "payment"
    OPERATIONS = "operations"


class NodeType(str, Enum):
    PATIENT = "patient"
    ENCOUNTER = "encounter"
    PROVIDER = "provider"
    RECORD = "record"


class EdgeType(str, Enum):
    HAS_ENCOUNTER = "has_encounter"  # patient   -> encounter
    SEEN_BY = "seen_by"              # encounter -> provider  (PROJECTED edge)
    HAS_RECORD = "has_record"        # encounter -> record


class DenialReason(str, Enum):
    """Coarse, PHI-free categories. Distinct values aid debugging without
    revealing which patient/actor was involved."""

    UNKNOWN_ACTOR = "unknown_actor"
    ACTION_NOT_PERMITTED = "action_not_permitted"
    PURPOSE_NOT_PERMITTED = "purpose_not_permitted"
    NOT_AUTHORIZED = "not_authorized"
    POLICY_ERROR = "policy_error"


# --------------------------------------------------------------------------- #
# Limits (bounded by construction)
# --------------------------------------------------------------------------- #
class GraphLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_encounters: int = 50
    max_records: int = 500
    max_nodes: int = 750
    max_edges: int = 1500


# --------------------------------------------------------------------------- #
# Authorization contracts
# --------------------------------------------------------------------------- #
class AuthorizationRequest(BaseModel):
    """A code-supplied access request. `patient_id` is the REQUESTED patient;
    it is the authorizer's job to decide whether `actor_id` may access it. In
    Stage 3 this is populated by deterministic code, never by model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    patient_id: int
    action: Action
    purpose: Purpose
    correlation_id: Optional[str] = None


class AuthorizedScope(BaseModel):
    """The trusted result of an ALLOW decision — the only key that unlocks
    retrieval. Immutable; a reader/repository binds to `patient_id` from here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    patient_id: int
    action: Action
    purpose: Purpose
    correlation_id: str


class Denial(BaseModel):
    """A DENY decision. Carries no actor id, patient id, name, or free text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: DenialReason
    correlation_id: str


# --------------------------------------------------------------------------- #
# Read-model rows (minimum-necessary; NO record bodies)
# --------------------------------------------------------------------------- #
class EncounterRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    patient_id: int
    encounter_type: Optional[str] = None
    provider: Optional[str] = None  # free text — the source of the projected Provider node
    status: Optional[str] = None


class RecordRow(BaseModel):
    """Minimum-necessary record projection. There is deliberately NO `body`
    field: the graph never needs the clinical narrative, so the read model does
    not load it and it cannot leak into a node, an evidence handle, or a log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    encounter_id: int
    patient_id: int
    kind: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None


class ChartResult(BaseModel):
    """Grouped output of a single bounded read (encounters + their records)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    patient_id: int
    encounters: list[EncounterRow]
    records: list[RecordRow]
    reads: int  # logical queries used (<= 2); never grows per-encounter
    truncated: bool = False


# --------------------------------------------------------------------------- #
# Graph contracts
# --------------------------------------------------------------------------- #
class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str  # evidence handle, e.g. "encounter:1"
    node_type: NodeType
    patient_id: int
    attributes: dict = Field(default_factory=dict)  # minimum-necessary, never a record body
    projected: bool = False
    provenance: Optional[str] = None


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    target_id: str
    edge_type: EdgeType


class PatientGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    patient_id: int
    correlation_id: str
    nodes: list[GraphNode]
    edges: list[Edge]
    evidence_ids: list[str]  # node ids usable as citations
    reads: int
    truncated: bool = False
    dropped_dangling: int = 0  # records whose encounter_id was not in scope, dropped safely
