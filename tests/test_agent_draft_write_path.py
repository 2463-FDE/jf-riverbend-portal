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
