"""create -> validate -> approve -> display, and the orderings that must hold.

The safety property is not any single transition, it is the ordering between
them: a patient sees a draft only when it was validated AND approved, and what
they see is the exact stored text of that version. `adr/0010` records why the
text is stored at all — a model response is not reproducible, so regenerating at
display could show text no clinician approved.

These run against SQLite, so migration 020's TRIGGER and CHECK are not exercised
here — those are database-level and were verified against live Postgres. What
these cover is the application state machine, which is where a wrong transition
would actually be written.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

# Load ONLY agent_drafts. It imports `models` itself, and loading models
# separately would register `patients` twice in the same MetaData.
drafts = load_module("services/records-service/agent_drafts.py", "agent_drafts_mod")

from libs.agent_provenance import ForbiddenPayload, Stage, TraceRecorder  # noqa: E402

TEXT_V1 = "Your A1c is 6.2%, down from 7.5% in March."
TEXT_V2 = "Your A1c is 6.2%, down 1.3 points since March."
CORR = "corr-test-1"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    drafts.AgentDraftProvenance.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _create(db, text=TEXT_V1, label=drafts.LABEL_REAL, model_id="model-x", citations=()):
    return drafts.create_draft(
        db, patient_id=1042, generated_text=text, correlation_id=CORR,
        provenance_label=label, model_id=model_id, prompt_version="v1",
        citations=citations,
    )


# --- create ----------------------------------------------------------------- #


def test_versions_are_monotonic_per_patient(db):
    v1 = _create(db)
    v2 = _create(db, text=TEXT_V2)

    assert (v1.version, v2.version) == (1, 2)
    assert v1.generated_text == TEXT_V1, "version 1's text is untouched by a regeneration"


def test_a_new_draft_is_not_displayable(db):
    _create(db)

    assert drafts.approved_draft(db, 1042) is None, "default deny — a draft is not approved"


def test_an_unlabelled_or_unknown_label_is_refused(db):
    with pytest.raises(drafts.DraftError, match="provenance_label"):
        drafts.create_draft(db, patient_id=1042, generated_text=TEXT_V1,
                            correlation_id=CORR, provenance_label="probably-real")


def test_a_fallback_draft_may_not_name_a_model(db):
    """A fallback did not call a model, so naming one misattributes the text —
    the exact confusion the labels exist to prevent."""
    with pytest.raises(drafts.DraftError, match="fallback"):
        _create(db, label=drafts.LABEL_FALLBACK, model_id="model-x")


def test_an_empty_draft_is_refused(db):
    with pytest.raises(drafts.DraftError):
        _create(db, text="   ")


def test_citations_are_stored_with_their_source_version(db):
    draft = _create(db, citations=[
        {"source_id": "doc-1", "source_version": "v2", "citation_id": "c1", "category": "lab"},
    ])

    cites = drafts.citations_for(db, draft.id)
    assert [(c.source_id, c.source_version, c.citation_id) for c in cites] == [
        ("doc-1", "v2", "c1")
    ]


# --- validate --------------------------------------------------------------- #


def test_validation_passing_makes_a_draft_decidable(db):
    draft = drafts.record_validation(db, _create(db), passed=True)

    assert draft.status == drafts.VALIDATED


def test_a_refused_draft_records_its_reason_code(db):
    draft = drafts.record_validation(db, _create(db), passed=False,
                                     validation_code="UNSUPPORTED_CLAIM")

    assert draft.status == drafts.REFUSED
    assert draft.validation_code == "UNSUPPORTED_CLAIM"


def test_a_refused_draft_can_never_be_approved(db):
    """If a refusal were approvable, deterministic validation would be advisory.
    The client's requirement is that it gates."""
    draft = drafts.record_validation(db, _create(db), passed=False, validation_code="X")

    with pytest.raises(drafts.DraftError, match="validated"):
        drafts.decide(db, draft, approve=True, reviewed_by=13)

    assert drafts.approved_draft(db, 1042) is None


def test_validation_cannot_be_replayed(db):
    draft = drafts.record_validation(db, _create(db), passed=True)

    with pytest.raises(drafts.DraftError):
        drafts.record_validation(db, draft, passed=False, validation_code="flip")


# --- approve / reject ------------------------------------------------------- #


def test_approval_names_the_decider_and_the_exact_version(db):
    draft = drafts.decide(db, drafts.record_validation(db, _create(db), passed=True),
                          approve=True, reviewed_by=13)

    assert draft.status == drafts.APPROVED
    assert draft.reviewed_by == 13
    assert draft.approved_at is not None and draft.rejected_at is None


def test_a_rejected_version_never_displays(db):
    drafts.decide(db, drafts.record_validation(db, _create(db), passed=True),
                  approve=False, reviewed_by=13)

    assert drafts.approved_draft(db, 1042) is None


def test_an_undecided_draft_cannot_be_decided_twice(db):
    draft = drafts.decide(db, drafts.record_validation(db, _create(db), passed=True),
                          approve=True, reviewed_by=13)

    with pytest.raises(drafts.DraftError):
        drafts.decide(db, draft, approve=False, reviewed_by=99)


# --- display ---------------------------------------------------------------- #


def test_the_patient_sees_the_exact_stored_text_of_the_approved_version(db):
    drafts.decide(db, drafts.record_validation(db, _create(db), passed=True),
                  approve=True, reviewed_by=13)

    shown = drafts.approved_draft(db, 1042)
    assert shown.generated_text == TEXT_V1, "displayed verbatim, never regenerated"
    assert shown.version == 1


def test_approving_v2_supersedes_v1_so_the_approved_version_is_unambiguous(db):
    """Two approved versions would make "the approved version" a guess, and
    display would have to pick one."""
    v1 = drafts.decide(db, drafts.record_validation(db, _create(db), passed=True),
                       approve=True, reviewed_by=13)
    v2 = drafts.decide(db, drafts.record_validation(db, _create(db, text=TEXT_V2), passed=True),
                       approve=True, reviewed_by=13)

    shown = drafts.approved_draft(db, 1042)
    assert shown.version == v2.version
    assert shown.generated_text == TEXT_V2
    db.refresh(v1)
    assert v1.status == drafts.SUPERSEDED
    assert v1.generated_text == TEXT_V1, "a superseded version keeps its text on the record"


def test_a_pending_v2_does_not_replace_an_approved_v1(db):
    """The demo beat: regenerate, and the patient still sees v1 until v2 is
    approved."""
    drafts.decide(db, drafts.record_validation(db, _create(db), passed=True),
                  approve=True, reviewed_by=13)
    _create(db, text=TEXT_V2)  # v2 created, not validated, not approved

    shown = drafts.approved_draft(db, 1042)
    assert shown.version == 1 and shown.generated_text == TEXT_V1


def test_another_patient_sees_nothing(db):
    drafts.decide(db, drafts.record_validation(db, _create(db), passed=True),
                  approve=True, reviewed_by=13)

    assert drafts.approved_draft(db, 1330) is None


# --- the label survives to display ----------------------------------------- #


@pytest.mark.parametrize("label,model", [
    (drafts.LABEL_REAL, "model-x"),
    (drafts.LABEL_FIXTURE, "model-x"),
    (drafts.LABEL_FALLBACK, None),
])
def test_the_provenance_label_reaches_the_displayed_version(db, label, model):
    drafts.decide(db, drafts.record_validation(db, _create(db, label=label, model_id=model),
                                               passed=True), approve=True, reviewed_by=13)

    assert drafts.approved_draft(db, 1042).provenance_label == label


# --- the trace, threaded through the real transitions ----------------------- #
#
# The client requires ONE privacy-safe trace covering request, agent decisions,
# retrieval outcome, provider call, validation, draft version, review decision
# and approved display. These assert it against the actual write path rather
# than against a hand-built recorder, because a trace that only holds together
# in a unit test is not evidence.

CITES = [
    {"source_id": "doc-1", "source_version": "v2", "citation_id": "c1", "category": "lab"},
    {"source_id": "doc-1", "source_version": "v2", "citation_id": "c2", "category": "lab"},
    {"source_id": "doc-2", "source_version": "v1", "citation_id": "c3", "category": "vitals"},
]


def test_one_trace_covers_all_seven_stages_through_the_real_path(db):
    trace = TraceRecorder(correlation_id=CORR)
    trace.request(actor_role="patient")
    trace.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")

    draft = drafts.create_draft(
        db, patient_id=1042, generated_text=TEXT_V1, correlation_id=CORR,
        provenance_label=drafts.LABEL_REAL, model_id="model-x", prompt_version="v1",
        citations=CITES, trace=trace,
    )
    drafts.record_validation(db, draft, passed=True, trace=trace)
    drafts.decide(db, draft, approve=True, reviewed_by=13, trace=trace)
    drafts.approved_draft(db, 1042, trace=trace)

    assert trace.is_complete(), f"missing stages: {trace.missing_stages()}"


def test_the_trace_carries_citation_ids_and_counts_not_content(db):
    trace = TraceRecorder(correlation_id=CORR)
    drafts.create_draft(
        db, patient_id=1042, generated_text=TEXT_V1, correlation_id=CORR,
        provenance_label=drafts.LABEL_REAL, model_id="model-x", citations=CITES, trace=trace,
    )

    retrieval = next(e for e in trace.events if e.stage is Stage.RETRIEVAL)
    assert retrieval.attributes["citation_ids"] == ["c1", "c2", "c3"]
    assert retrieval.attributes["document_count"] == 2, "two distinct sources, three citations"
    assert retrieval.attributes["categories"] == ["lab", "vitals"]


def test_no_stage_of_the_real_path_ever_carries_the_draft_text(db):
    """The boundary, asserted end to end. The text is in the database; it is not
    in the trace, and the guard would have raised if any stage tried."""
    trace = TraceRecorder(correlation_id=CORR)
    draft = drafts.create_draft(
        db, patient_id=1042, generated_text=TEXT_V1, correlation_id=CORR,
        provenance_label=drafts.LABEL_REAL, model_id="model-x", citations=CITES, trace=trace,
    )
    drafts.record_validation(db, draft, passed=True, trace=trace)
    drafts.decide(db, draft, approve=True, reviewed_by=13, trace=trace)
    drafts.approved_draft(db, 1042, trace=trace)

    serialised = repr([e.attributes for e in trace.events])
    assert TEXT_V1 not in serialised
    assert "A1c" not in serialised, "not even a fragment of the clinical text"


def test_the_display_stage_names_the_version_that_was_shown(db):
    trace = TraceRecorder(correlation_id=CORR)
    draft = _create(db)
    drafts.record_validation(db, draft, passed=True)
    drafts.decide(db, draft, approve=True, reviewed_by=13)
    drafts.approved_draft(db, 1042, trace=trace)

    shown = next(e for e in trace.events if e.stage is Stage.DISPLAY)
    assert shown.attributes["draft_version"] == 1
    assert shown.attributes["provenance_label"] == drafts.LABEL_REAL


def test_nothing_is_traced_when_there_is_nothing_approved_to_show(db):
    """A display event for a patient who was shown nothing would be a false
    record of a disclosure."""
    trace = TraceRecorder(correlation_id=CORR)
    _create(db)  # created, never approved

    assert drafts.approved_draft(db, 1042, trace=trace) is None
    assert not any(e.stage is Stage.DISPLAY for e in trace.events)


def test_a_refusal_is_traced_with_its_code_and_no_review_follows(db):
    trace = TraceRecorder(correlation_id=CORR)
    draft = _create(db)
    drafts.record_validation(db, draft, passed=False, validation_code="UNSUPPORTED_CLAIM",
                             trace=trace)

    validation = next(e for e in trace.events if e.stage is Stage.VALIDATION)
    assert validation.attributes["passed"] is False
    assert validation.attributes["validation_code"] == "UNSUPPORTED_CLAIM"
    assert not any(e.stage is Stage.REVIEW for e in trace.events)


def test_the_provider_stage_records_the_prompt_version_not_the_prompt(db):
    trace = TraceRecorder(correlation_id=CORR)
    drafts.create_draft(
        db, patient_id=1042, generated_text=TEXT_V1, correlation_id=CORR,
        provenance_label=drafts.LABEL_REAL, model_id="model-x", prompt_version="v3",
        trace=trace,
    )

    call = next(e for e in trace.events if e.stage is Stage.PROVIDER_CALL)
    assert call.attributes["prompt_version"] == "v3"
    assert call.attributes["model_id"] == "model-x"
    assert "prompt" not in call.attributes


def test_a_fallback_draft_traces_as_fallback_with_no_model(db):
    trace = TraceRecorder(correlation_id=CORR)
    drafts.create_draft(
        db, patient_id=1042, generated_text=TEXT_V1, correlation_id=CORR,
        provenance_label=drafts.LABEL_FALLBACK, model_id=None, trace=trace,
    )

    call = next(e for e in trace.events if e.stage is Stage.PROVIDER_CALL)
    assert call.attributes["provenance_label"] == "fallback"
    assert call.attributes["model_id"] is None


def test_the_write_path_works_without_a_trace_at_all(db):
    """Tracing is threaded, not required. A caller that passes no recorder must
    still get correct behaviour — otherwise observability becomes a dependency
    of correctness."""
    draft = _create(db)
    drafts.record_validation(db, draft, passed=True)
    drafts.decide(db, draft, approve=True, reviewed_by=13)

    assert drafts.approved_draft(db, 1042).version == 1
