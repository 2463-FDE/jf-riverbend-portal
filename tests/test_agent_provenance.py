"""The trace and provenance spine for the September 2 agentic path.

The privacy property is the point. The client's constraint is absolute — never
persist prompts, model output, retrieved text, patient data, identifiers,
credentials or raw provider errors — and a convention cannot hold that line.
Someone adds `prompt=` for a day's debugging and it ships.

So these tests assert the guard is *enforced*, that failing loudly is the chosen
behaviour, and that the safe path still carries enough to be useful. A trace
nobody can read is not privacy, it is just absence.
"""
import pytest

from libs.agent_provenance import (
    FORBIDDEN_KEYS,
    STAGES,
    ForbiddenPayload,
    ProvenanceLabel,
    Stage,
    TraceRecorder,
    assert_safe,
)


def _rec():
    return TraceRecorder(correlation_id="corr-1")


def _full(t):
    t.request(actor_role="patient")
    t.retrieval(document_count=2, citation_ids=["c1", "c2"], categories=["lab"], excluded_count=1)
    t.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x", latency_ms=120)
    t.draft(draft_version=1, label=ProvenanceLabel.REAL, model_id="model-x",
            prompt_version="v3", citation_ids=["c1"])
    # migration 020's agent_draft_validation_code_consistent CHECK requires
    # exactly 'PASS' for every non-refused post-validation status — a fixture
    # that used None here (as this once did) would model a state the real
    # schema now rejects outright.
    t.validation(passed=True, validation_code="PASS", citation_ids=["c1"])
    t.review(decision="approved", draft_version=1, decided_by_user_id=13)
    t.display(draft_version=1, label=ProvenanceLabel.REAL)
    return t


# --- the guard -------------------------------------------------------------- #


@pytest.mark.parametrize("key", [
    "prompt", "system_prompt", "messages", "completion", "response",
    "model_output", "draft_text", "summary_text", "content", "raw_response",
    "retrieved_text", "document", "chunk", "passage", "context",
    "ssn", "dob", "mrn", "name", "patient_name", "address", "email", "notes",
    "password", "api_key", "authorization", "aws_secret_access_key",
    "internal_service_token", "bedrock_token",
    "error", "error_message", "traceback",
])
def test_a_forbidden_attribute_raises(key):
    with pytest.raises(ForbiddenPayload):
        _rec().record(Stage.PROVIDER_CALL, **{key: "anything"})


def test_the_guard_is_case_insensitive():
    with pytest.raises(ForbiddenPayload):
        _rec().record(Stage.REQUEST, PROMPT="x")


def test_failing_loudly_is_the_chosen_behaviour():
    """A dropped trace is recoverable; a leaked prompt in an aggregator is not.
    So the guard raises rather than silently dropping the attribute — a silent
    drop would let the caller believe the value was recorded."""
    t = _rec()

    with pytest.raises(ForbiddenPayload):
        t.record(Stage.PROVIDER_CALL, model_id="m", prompt="leak")

    assert t.events == [], "nothing is recorded when any attribute is rejected"


def test_the_error_message_names_the_offenders_and_the_alternative():
    with pytest.raises(ForbiddenPayload) as exc:
        _rec().record(Stage.PROVIDER_CALL, prompt="x", ssn="y")

    message = str(exc.value)
    assert "prompt" in message and "ssn" in message
    assert "citation id" in message, "must tell the caller what to record instead"


def test_assert_safe_allows_the_permitted_metadata():
    # The whole permitted vocabulary from the client's list.
    assert_safe({
        "source_id": "doc-1", "source_version": "v2", "citation_ids": ["c1"],
        "categories": ["lab"], "status": "approved", "correlation_id": "corr-1",
        "created_at": "2026-09-02T00:00:00Z", "model_id": "model-x",
        "provenance_label": "real", "validation_code": "UNSUPPORTED_CLAIM",
        "draft_version": 1, "decided_by_user_id": 13, "error_type": "ClientError",
    })


def test_forbidden_keys_does_not_accidentally_ban_permitted_names():
    permitted = {
        "source_id", "source_version", "citation_ids", "categories", "status",
        "correlation_id", "model_id", "provenance_label", "validation_code",
        "draft_version", "decided_by_user_id", "error_type", "tool_name",
        "document_count", "excluded_count", "latency_ms", "actor_role",
    }
    assert not (permitted & FORBIDDEN_KEYS)


# --- what the stages carry -------------------------------------------------- #


def test_one_trace_covers_all_eight_stages_in_order():
    """The client requires ONE trace across the whole path, so a partial trace
    is a failed acceptance criterion rather than a partial success."""
    t = _full(_rec())

    assert t.is_complete()
    assert t.is_ordered()
    assert t.is_acceptable()
    assert t.missing_stages() == []
    assert len(STAGES) == 8


def test_an_incomplete_trace_names_what_is_missing():
    t = _rec()
    t.request(actor_role="patient")

    assert not t.is_complete()
    assert "draft" in t.missing_stages()
    assert "provider_call" in t.missing_stages()
    assert "display" in t.missing_stages()


def test_stages_out_of_canonical_order_are_detected():
    """The trace must cover the path IN ORDER; a display before a request is a
    broken sequence even if every stage eventually appears."""
    t = _rec()
    t.display(draft_version=1, label=ProvenanceLabel.REAL)
    t.request(actor_role="patient")

    assert not t.is_ordered()
    assert not t.is_acceptable()


# --- the loop exception: only agent_decision/provider_call may repeat, and
# only before draft ----------------------------------------------------------- #


def test_a_genuine_tool_loop_repeating_before_draft_is_ordered():
    """A bounded tool-calling loop can decide and call the model more than
    once while gathering evidence — that is the actual shape of the required
    path, not a violation of it."""
    t = _rec()
    t.request(actor_role="patient")
    t.retrieval(document_count=2, citation_ids=["c1"], categories=["lab"])
    t.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")
    t.agent_decision(tool_name="search_documents", turn=2, stop_reason="tool_use")
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")
    t.draft(draft_version=1, label=ProvenanceLabel.REAL, model_id="model-x",
            prompt_version="v3", citation_ids=["c1"])
    t.validation(passed=True, validation_code="PASS", citation_ids=["c1"])
    t.review(decision="approved", draft_version=1, decided_by_user_id=13)
    t.display(draft_version=1, label=ProvenanceLabel.REAL)

    assert t.is_ordered()
    assert t.is_acceptable()


@pytest.mark.parametrize("loop_stage", ["agent_decision", "provider_call"])
def test_a_loop_stage_repeating_after_draft_is_rejected(loop_stage):
    """A repeat of the loop AFTER draft would mean the drafted version was not
    actually produced by the evidence/decisions the trace claims preceded it —
    an ordering failure, not a tolerated retry."""
    t = _full(_rec())
    if loop_stage == "agent_decision":
        t.agent_decision(tool_name="search_documents", turn=3, stop_reason="tool_use")
    else:
        t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")

    assert not t.is_ordered()
    assert not t.is_acceptable()


@pytest.mark.parametrize("loop_stage", ["agent_decision", "provider_call"])
def test_a_loop_stage_before_retrieval_is_rejected(loop_stage):
    """The loop exception only covers the actual loop position — after
    retrieval, before draft. A loop stage as the very first event (before
    retrieval has even happened) is out of place, not an early iteration."""
    t = _rec()
    if loop_stage == "agent_decision":
        t.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")
    else:
        t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")
    t.request(actor_role="patient")
    t.retrieval(document_count=1, citation_ids=["c1"], categories=["lab"])

    assert not t.is_ordered()


@pytest.mark.parametrize("repeated_stage,call", [
    ("retrieval", lambda t: t.retrieval(document_count=1, citation_ids=["c1"], categories=["lab"])),
    ("validation", lambda t: t.validation(passed=True, validation_code="PASS", citation_ids=["c1"])),
    ("review", lambda t: t.review(decision="approved", draft_version=1, decided_by_user_id=13)),
    ("display", lambda t: t.display(draft_version=1, label=ProvenanceLabel.REAL)),
])
def test_a_non_loop_stage_repeating_is_rejected_even_in_order(repeated_stage, call):
    """Only agent_decision/provider_call are allowed to repeat. A second
    retrieval, validation, review or display — even appended at the END,
    where a naive first-seen-order check would have missed it — is invalid."""
    t = _full(_rec())
    call(t)

    assert not t.is_ordered()
    assert not t.is_acceptable()


def test_the_draft_stage_references_the_version_never_the_text():
    """The eighth stage: the versioned draft is recorded by reference (version,
    provenance, model/prompt version, citations) — never its text."""
    t = _rec()
    event = t.draft(draft_version=2, label=ProvenanceLabel.REAL, model_id="model-x",
                    prompt_version="v3", citation_ids=["c1"])

    assert event.stage is Stage.DRAFT
    assert event.attributes["draft_version"] == 2
    assert event.attributes["prompt_version"] == "v3"
    assert not any(k in event.attributes for k in ("generated_text", "text", "draft_text"))


def test_retrieval_records_counts_and_ids_not_documents():
    t = _rec()
    event = t.retrieval(document_count=3, citation_ids=["c1"], categories=["lab"],
                        excluded_count=2)

    assert event.attributes["document_count"] == 3
    assert event.attributes["excluded_count"] == 2, "an excluded doc is evidence the bound worked"
    assert "document" not in event.attributes and "context" not in event.attributes


def test_provider_call_accepts_an_error_TYPE_but_not_a_message():
    t = _rec()
    t.provider_call(label=ProvenanceLabel.FALLBACK, model_id=None,
                    error_type="ModuleNotFoundError")

    assert t.events[-1].attributes["error_type"] == "ModuleNotFoundError"
    with pytest.raises(ForbiddenPayload):
        t.record(Stage.PROVIDER_CALL, error_message="No module named 'boto3'")


def test_review_records_a_user_id_never_a_name():
    t = _rec()
    t.review(decision="approved", draft_version=2, decided_by_user_id=13)

    assert t.events[-1].attributes["decided_by_user_id"] == 13
    with pytest.raises(ForbiddenPayload):
        t.record(Stage.REVIEW, name="Dr. Grace Kim")


# --- the three labels ------------------------------------------------------- #


def test_the_three_provenance_labels_are_exactly_the_clients_three():
    assert {l.value for l in ProvenanceLabel} == {"real", "fixture", "fallback"}


def test_a_fallback_display_is_labelled_as_such():
    """Fallback text must never be presented as model output."""
    t = _rec()
    t.display(draft_version=1, label=ProvenanceLabel.FALLBACK)

    assert t.events[-1].attributes["provenance_label"] == "fallback"


def test_the_label_survives_from_provider_call_to_display():
    t = _full(_rec())
    call = next(e for e in t.events if e.stage is Stage.PROVIDER_CALL)
    shown = next(e for e in t.events if e.stage is Stage.DISPLAY)

    assert call.attributes["provenance_label"] == shown.attributes["provenance_label"]


def test_the_correlation_id_is_one_value_for_the_whole_request():
    t = _full(_rec())

    assert t.correlation_id == "corr-1"
    assert len(t.events) == 8


# --- the persistence boundary (adr/0010, decision A) ------------------------ #
#
# The draft text IS persisted, as a clinical artifact. The prohibition is scoped
# to telemetry. These tests pin the telemetry half — the schema half is enforced
# by migration 020's trigger and CHECK, verified against a live database.


@pytest.mark.parametrize("key", [
    "generated_text", "draft_text", "summary_text", "text", "content",
    "output", "model_output", "completion", "response", "generated",
])
def test_draft_text_can_never_reach_a_trace_under_any_name(key):
    """adr/0010's boundary, from the telemetry side. The artifact is stored; the
    telemetry copy is what is forbidden, and it must stay forbidden under every
    plausible attribute name someone might reach for."""
    with pytest.raises(ForbiddenPayload):
        _rec().record(Stage.DISPLAY, **{key: "A1c 6.2%, down from 7.5% in March."})


def test_the_prompt_itself_is_forbidden_but_its_version_is_not():
    """Only prompt_version is persisted or traced. The prompt text stays out of
    the database exactly as it stays out of the trace."""
    t = _rec()
    t.record(Stage.PROVIDER_CALL, prompt_version="v3")

    assert t.events[-1].attributes["prompt_version"] == "v3"
    with pytest.raises(ForbiddenPayload):
        t.record(Stage.PROVIDER_CALL, prompt="You are a clinical summariser...")


def test_a_display_event_references_a_version_rather_than_carrying_the_text():
    """This is the shape the whole boundary depends on: telemetry says WHICH
    version was shown, and the artifact table says what that version says."""
    t = _rec()
    event = t.display(draft_version=2, label=ProvenanceLabel.REAL)

    assert event.attributes["draft_version"] == 2
    assert not any(k in event.attributes for k in ("generated_text", "text", "content"))


def test_prompt_version_is_not_accidentally_banned():
    # The guard must not be so broad that the permitted metadata is unusable.
    assert "prompt_version" not in FORBIDDEN_KEYS
    assert "draft_version" not in FORBIDDEN_KEYS
