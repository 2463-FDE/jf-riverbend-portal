"""Stage 2 — seeded graph core tests.

Covers: same-patient-only assembly + evidence ids, cross-patient evidence
rejection, missing (dangling) node drop, duplicate-node dedupe,
provider-projection labeling, row and traversal caps, deterministic ordering,
bounded single-read/query-count behavior, and the no-record-body invariant.
"""
import pytest

from libs.patient_view_agent import (
    Action,
    AuthorizedScope,
    ChartRepositoryPort,
    ChartResult,
    CrossPatientEvidenceError,
    EncounterRow,
    GraphLimits,
    NodeType,
    PatientGraphReader,
    Purpose,
    RecordRow,
    SeededChartRepository,
    seed_derived_sample,
)


def scope(pid=1042, cid="cid-graph"):
    return AuthorizedScope(
        actor_id="actor",
        patient_id=pid,
        action=Action.VIEW_PATIENT_CHART,
        purpose=Purpose.TREATMENT,
        correlation_id=cid,
    )


def _ids(graph):
    return {n.node_id for n in graph.nodes}


def test_assembles_only_same_patient_nodes_with_evidence_ids():
    repo = SeededChartRepository(*seed_derived_sample())
    graph = PatientGraphReader(scope(1042), repo).build()

    ids = _ids(graph)
    assert "patient:1042" in ids
    assert {"encounter:1", "encounter:6"} <= ids
    assert {"record:1", "record:6"} <= ids
    assert {"provider:dr-patel", "provider:dr-grace-kim"} <= ids

    # Every node belongs to the bound patient; no 1043 (James O'Brien) leakage.
    assert all(n.patient_id == 1042 for n in graph.nodes)
    assert "encounter:4" not in ids and "encounter:7" not in ids
    assert "record:4" not in ids and "record:7" not in ids

    # Evidence handles == node ids, and every assertion is citable.
    assert graph.evidence_ids == [n.node_id for n in graph.nodes]
    assert len(graph.evidence_ids) == len(graph.nodes) > 0


def test_edges_link_patient_encounter_provider_record():
    repo = SeededChartRepository(*seed_derived_sample())
    graph = PatientGraphReader(scope(1042), repo).build()
    edges = {(e.source_id, e.target_id, e.edge_type.value) for e in graph.edges}
    assert ("patient:1042", "encounter:1", "has_encounter") in edges
    assert ("encounter:1", "provider:dr-patel", "seen_by") in edges
    assert ("encounter:1", "record:1", "has_record") in edges
    # No Provider->Record authorship edge is asserted (schema cannot prove it).
    assert not any(e.source_id.startswith("provider:") for e in graph.edges)


def test_record_nodes_never_carry_a_body():
    repo = SeededChartRepository(*seed_derived_sample())
    graph = PatientGraphReader(scope(1042), repo).build()
    record_nodes = [n for n in graph.nodes if n.node_type == NodeType.RECORD]
    assert record_nodes
    for n in record_nodes:
        assert "body" not in n.attributes
        assert set(n.attributes) <= {"kind", "title", "status"}
    # The read model itself has no body field — leakage is impossible by shape.
    assert "body" not in RecordRow.model_fields


def test_provider_nodes_are_labeled_as_projections():
    repo = SeededChartRepository(*seed_derived_sample())
    graph = PatientGraphReader(scope(1042), repo).build()
    providers = [n for n in graph.nodes if n.node_type == NodeType.PROVIDER]
    assert providers
    for n in providers:
        assert n.projected is True
        assert n.provenance and "free text" in n.provenance


def test_cross_patient_record_is_rejected_fail_closed():
    # A record that claims a different patient than the one it hangs off of.
    encounters = [EncounterRow(id=1, patient_id=1042, provider="Dr. Patel")]
    records = [RecordRow(id=99, encounter_id=1, patient_id=1043, kind="note", title="x", status="final")]
    repo = SeededChartRepository(encounters, records)
    with pytest.raises(CrossPatientEvidenceError):
        PatientGraphReader(scope(1042), repo).build()


class _OrphanRepo(ChartRepositoryPort):
    """Returns a record whose encounter_id is not among the returned encounters
    — exercises the graph's dangling-record drop (a missing node)."""

    def load_chart(self, patient_id, *, correlation_id=""):
        return ChartResult(
            patient_id=patient_id,
            encounters=[EncounterRow(id=1, patient_id=patient_id, provider="Dr. Patel")],
            records=[RecordRow(id=5, encounter_id=2, patient_id=patient_id, kind="note", title="x", status="final")],
            reads=2,
        )


def test_record_for_missing_encounter_is_dropped_not_fabricated():
    graph = PatientGraphReader(scope(1042), _OrphanRepo()).build()
    assert graph.dropped_dangling == 1
    ids = _ids(graph)
    assert "record:5" not in ids
    assert "encounter:2" not in ids  # no phantom encounter fabricated


def test_duplicate_rows_are_deduped_by_id():
    encounters = [
        EncounterRow(id=1, patient_id=1042, provider="Dr. Patel"),
        EncounterRow(id=1, patient_id=1042, provider="Dr. Patel"),
    ]
    records = [
        RecordRow(id=5, encounter_id=1, patient_id=1042, kind="note", title="x", status="final"),
        RecordRow(id=5, encounter_id=1, patient_id=1042, kind="note", title="x", status="final"),
    ]
    graph = PatientGraphReader(scope(1042), SeededChartRepository(encounters, records)).build()
    assert len([n for n in graph.nodes if n.node_type == NodeType.ENCOUNTER]) == 1
    assert len([n for n in graph.nodes if n.node_type == NodeType.RECORD]) == 1


def test_repository_row_cap_truncates():
    encounters = [EncounterRow(id=i, patient_id=1042, provider=f"Dr. {i}") for i in range(1, 101)]
    limits = GraphLimits(max_encounters=10)
    repo = SeededChartRepository(encounters, [], limits=limits)
    graph = PatientGraphReader(scope(1042), repo, limits=limits).build()
    assert graph.truncated is True
    assert len([n for n in graph.nodes if n.node_type == NodeType.ENCOUNTER]) == 10


def test_node_cap_truncates_without_dangling_edges():
    encounters = [EncounterRow(id=i, patient_id=1042, provider=f"Dr. {i}") for i in range(1, 101)]
    # Let the repository return everything; cap the graph at the node layer.
    repo = SeededChartRepository(encounters, [], limits=GraphLimits(max_encounters=1000))
    graph = PatientGraphReader(scope(1042), repo, limits=GraphLimits(max_nodes=5)).build()
    assert graph.truncated is True
    assert len(graph.nodes) <= 5
    node_ids = _ids(graph)
    # every edge endpoint must be a real node (no dangling edge from truncation)
    for e in graph.edges:
        assert e.source_id in node_ids and e.target_id in node_ids


def test_deterministic_ordering_is_stable_and_sorted():
    a = PatientGraphReader(scope(1042), SeededChartRepository(*seed_derived_sample())).build()
    b = PatientGraphReader(scope(1042), SeededChartRepository(*seed_derived_sample())).build()
    ids_a = [n.node_id for n in a.nodes]
    ids_b = [n.node_id for n in b.nodes]
    assert ids_a == ids_b  # stable across runs
    edges_a = [(e.source_id, e.target_id, e.edge_type.value) for e in a.edges]
    edges_b = [(e.source_id, e.target_id, e.edge_type.value) for e in b.edges]
    assert edges_a == edges_b
    # patient root first; encounters in ascending numeric order.
    assert ids_a[0] == "patient:1042"
    assert ids_a.index("encounter:1") < ids_a.index("encounter:6")


def test_query_count_is_constant_not_one_plus_n():
    few = [EncounterRow(id=i, patient_id=1042, provider="Dr. Patel") for i in range(1, 4)]
    many = [EncounterRow(id=i, patient_id=1042, provider="Dr. Patel") for i in range(1, 51)]
    big_limits = GraphLimits(max_encounters=1000)
    c_few = SeededChartRepository(few, [], limits=big_limits).load_chart(1042)
    c_many = SeededChartRepository(many, [], limits=big_limits).load_chart(1042)
    assert c_few.reads == 2
    assert c_many.reads == 2  # constant — did not grow with encounter count
    # and the count is surfaced on the graph
    graph = PatientGraphReader(scope(1042), SeededChartRepository(many, [], limits=big_limits)).build()
    assert graph.reads == 2


def test_reader_is_bound_to_scope_patient_only():
    # Two patients present; a reader bound to 1042 must never surface 1043 data,
    # and there is no build() argument that could change the patient.
    repo = SeededChartRepository(*seed_derived_sample())
    graph = PatientGraphReader(scope(1043), repo).build()
    assert graph.patient_id == 1043
    assert all(n.patient_id == 1043 for n in graph.nodes)
    assert {"encounter:4", "encounter:7"} <= _ids(graph)
    assert "encounter:1" not in _ids(graph)  # 1042's data absent
