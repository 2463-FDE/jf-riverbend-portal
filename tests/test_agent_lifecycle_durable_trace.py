"""W10 Final Stage 4 — services/records-service/agent_lifecycle.py, the
durable append-only sink replacing three separate, per-request, in-memory-
only TraceRecorder instances (generation, review, display).

Real Postgres concurrency/append-only enforcement (migration 036's
trigger) is proved separately, against a live database, in
tests/integration/test_agent_lifecycle_events_migration.py. This file is
the fast, DB-less (SQLite) unit coverage of persist()/reconstruct()'s own
contract and the verifier script's reporting logic.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

from libs.agent_provenance import ForbiddenPayload, ProvenanceLabel, Stage, StageEvent, TraceRecorder

agent_lifecycle = load_module("services/records-service/agent_lifecycle.py", "agent_lifecycle_mod")
verify = load_module("db/migrations/scripts/verify_agent_lifecycle.py", "verify_agent_lifecycle_mod")
AgentLifecycleEvent = agent_lifecycle.AgentLifecycleEvent


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AgentLifecycleEvent.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _full_trace(correlation_id="corr-1"):
    t = TraceRecorder(correlation_id)
    t.request(actor_role="clinician")
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x", latency_ms=100)
    t.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")
    t.retrieval(document_count=1, citation_ids=["c1"], categories=["lab"])
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x", latency_ms=90)
    t.agent_decision(tool_name=None, turn=2, stop_reason="end_turn")
    t.draft(draft_version=1, label=ProvenanceLabel.REAL, model_id="model-x",
            prompt_version="v3", citation_ids=["c1"])
    t.validation(passed=True, validation_code="PASS", citation_ids=["c1"])
    return t


def test_persisting_and_reconstructing_round_trips_the_same_shape(db):
    trace = _full_trace()
    agent_lifecycle.persist(db, trace.correlation_id, trace.events)
    db.commit()

    rebuilt = agent_lifecycle.reconstruct(db, trace.correlation_id)

    assert [e.stage for e in rebuilt.events] == [e.stage for e in trace.events]
    assert [e.attributes for e in rebuilt.events] == [e.attributes for e in trace.events]
    assert rebuilt.is_ordered()
    assert rebuilt.is_grounded()


def test_review_and_display_append_to_the_same_persisted_stream_across_calls(db):
    """The whole point: generation, review, and display happen in three
    SEPARATE calls (real routes: three separate HTTP requests) but must
    accumulate into ONE reconstructible stream."""
    generation = _full_trace("corr-shared")
    agent_lifecycle.persist(db, "corr-shared", generation.events)
    db.commit()

    review_trace = TraceRecorder("corr-shared")
    review_trace.review(decision="approved", draft_version=1)
    agent_lifecycle.persist(db, "corr-shared", review_trace.events)
    db.commit()

    display_trace = TraceRecorder("corr-shared")
    display_trace.display(draft_version=1, label=ProvenanceLabel.REAL)
    agent_lifecycle.persist(db, "corr-shared", display_trace.events)
    db.commit()

    rebuilt = agent_lifecycle.reconstruct(db, "corr-shared")

    assert rebuilt.is_complete()
    assert rebuilt.is_ordered()
    assert rebuilt.is_grounded()
    assert rebuilt.is_acceptable()
    assert len(rebuilt.events) == 10  # 8 full_trace + review + display


def test_persist_of_an_empty_event_list_is_a_no_op(db):
    agent_lifecycle.persist(db, "corr-empty", [])
    db.commit()

    rebuilt = agent_lifecycle.reconstruct(db, "corr-empty")
    assert rebuilt.events == []


def test_sequence_continues_across_separate_correlation_ids_independently(db):
    a = TraceRecorder("corr-a")
    a.request(actor_role="clinician")
    b = TraceRecorder("corr-b")
    b.request(actor_role="patient")

    agent_lifecycle.persist(db, "corr-a", a.events)
    agent_lifecycle.persist(db, "corr-b", b.events)
    db.commit()

    rows_a = db.query(AgentLifecycleEvent).filter_by(correlation_id="corr-a").all()
    rows_b = db.query(AgentLifecycleEvent).filter_by(correlation_id="corr-b").all()
    assert [r.sequence for r in rows_a] == [1]
    assert [r.sequence for r in rows_b] == [1]  # independent counters, not a shared global one


def test_persist_re_checks_the_safety_guard_and_never_reaches_the_database(db):
    """Defense in depth: persist() must not trust that every StageEvent it is
    handed was actually built through the guarded TraceRecorder API."""
    forbidden_event = StageEvent(stage=Stage.REVIEW, attributes={"name": "Dr. Grace Kim"})

    with pytest.raises(ForbiddenPayload):
        agent_lifecycle.persist(db, "corr-leak", [forbidden_event])

    assert db.query(AgentLifecycleEvent).filter_by(correlation_id="corr-leak").count() == 0


# --- the verifier's report() — pure formatting, no DB -----------------------


def test_report_names_the_full_grounded_path_as_acceptable():
    trace = _full_trace("corr-report")
    trace.review(decision="approved", draft_version=1)
    trace.display(draft_version=1, label=ProvenanceLabel.REAL)

    output = verify.report(trace)

    assert "is_complete: True" in output
    assert "is_ordered: True" in output
    assert "is_grounded: True" in output
    assert "is_acceptable (real/grounded path only): True" in output
    assert "corr-report" in output


def test_report_never_prints_attribute_values_only_stage_names():
    trace = TraceRecorder("corr-report-2")
    trace.provider_call(label=ProvenanceLabel.REAL, model_id="a-secret-looking-model-id-xyz")

    output = verify.report(trace)

    assert "a-secret-looking-model-id-xyz" not in output
    assert "provider_call" in output


def test_report_names_a_fallback_shape_as_such_not_as_a_failed_acceptable_trace():
    trace = TraceRecorder("corr-fallback")
    trace.request(actor_role="patient")
    trace.provider_call(label=ProvenanceLabel.FALLBACK, model_id=None, error_type="ConnectionError")
    trace.display(draft_version=1, label=ProvenanceLabel.FALLBACK)

    output = verify.report(trace)

    assert "is_acceptable (real/grounded path only): False" in output
    assert "genuinely shorter shape" in output
