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
    """The REAL, grounded path — one tool cycle, exactly the shape
    raw_bedrock.py/langchain_runtime.py actually produce: a provider_call
    always precedes the agent_decision derived from its response; retrieval
    follows only a tool_use decision; a second provider_call/agent_decision
    pair (this one final) closes the loop before drafting."""
    t.request(actor_role="patient")
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x", latency_ms=120)
    t.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")
    t.retrieval(document_count=2, citation_ids=["c1", "c2"], categories=["lab"], excluded_count=1)
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x", latency_ms=95)
    t.agent_decision(tool_name=None, turn=2, stop_reason="end_turn")
    t.draft(draft_version=1, label=ProvenanceLabel.REAL, model_id="model-x",
            prompt_version="v3", citation_ids=["c1"])
    # migration 020's agent_draft_validation_code_consistent CHECK requires
    # exactly 'PASS' for every non-refused post-validation status — a fixture
    # that used None here (as this once did) would model a state the real
    # schema now rejects outright.
    t.validation(passed=True, validation_code="PASS", citation_ids=["c1"])
    t.review(decision="approved", draft_version=1)
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


# --- ALC-NESTED-GUARD: the guard recurses into nested structures -----------


@pytest.mark.parametrize("stage,kwargs", [
    (Stage.REQUEST, {"metadata": {"user_id": 13}}),
    (Stage.RETRIEVAL, {"sources": [{"citation_id": "c1"}, {"name": "Jane Doe"}]}),
    (Stage.REQUEST, {"a": {"b": {"ssn": "123-45-6789"}}}),
])
def test_a_forbidden_key_nested_at_any_depth_raises(stage, kwargs):
    with pytest.raises(ForbiddenPayload):
        _rec().record(stage, **kwargs)


def test_the_nested_guard_never_reveals_the_forbidden_value():
    with pytest.raises(ForbiddenPayload) as exc:
        _rec().record(Stage.REQUEST, metadata={"user_id": "super-secret-id-42"})

    assert "super-secret-id-42" not in str(exc.value)


def test_valid_nested_citation_and_category_metadata_is_still_accepted():
    """The recursive guard must not make ordinary nested metadata unusable."""
    event = _rec().record(
        Stage.RETRIEVAL,
        sources=[{"citation_id": "c1", "category": "lab"}, {"citation_id": "c2", "category": "vitals"}],
        breakdown={"lab": 1, "vitals": 1},
    )

    assert event.attributes["sources"][0]["citation_id"] == "c1"
    assert event.attributes["breakdown"] == {"lab": 1, "vitals": 1}


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
        "draft_version": 1, "error_type": "ClientError",
    })


def test_forbidden_keys_does_not_accidentally_ban_permitted_names():
    permitted = {
        "source_id", "source_version", "citation_ids", "categories", "status",
        "correlation_id", "model_id", "provenance_label", "validation_code",
        "draft_version", "error_type", "tool_name",
        "document_count", "excluded_count", "latency_ms", "actor_role",
    }
    assert not (permitted & FORBIDDEN_KEYS)


# --- what the stages carry -------------------------------------------------- #


def test_one_trace_covers_the_real_grounded_path_in_order():
    """The client requires ONE trace across the whole path, so a partial trace
    is a failed acceptance criterion rather than a partial success."""
    t = _full(_rec())

    assert t.is_complete()
    assert t.is_ordered()
    assert t.is_grounded()
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


# --- the real loop grammar, grounded in raw_bedrock.py / langchain_runtime.py:
# request -> {provider_call -> agent_decision(tool_use) -> retrieval}* ->
# provider_call -> agent_decision(final) -> draft -> validation -> review ->
# display ---------------------------------------------------------------- #


def _tool_cycle(t, *, turn: int):
    """One (provider_call, agent_decision(tool_use), retrieval) cycle — the
    unit that repeats zero or more bounded times before the closing, final
    decision."""
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")
    t.agent_decision(tool_name="search_documents", turn=turn, stop_reason="tool_use")
    t.retrieval(document_count=1, citation_ids=[f"c{turn}"], categories=["lab"])
    return t


def _final_decision_through_display(t, *, turn: int):
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")
    t.agent_decision(tool_name=None, turn=turn, stop_reason="end_turn")
    t.draft(draft_version=1, label=ProvenanceLabel.REAL, model_id="model-x",
            prompt_version="v3", citation_ids=["c1"])
    t.validation(passed=True, validation_code="PASS", citation_ids=["c1"])
    t.review(decision="approved", draft_version=1)
    t.display(draft_version=1, label=ProvenanceLabel.REAL)
    return t


def test_one_tool_call_success():
    """Jorge's canonical example, exactly: request -> provider_call ->
    agent_decision(tool_use) -> retrieval -> provider_call ->
    agent_decision(final) -> draft -> validation -> review -> display."""
    t = _rec()
    t.request(actor_role="patient")
    _tool_cycle(t, turn=1)
    _final_decision_through_display(t, turn=2)

    assert t.is_ordered()
    assert t.is_grounded()
    assert t.is_acceptable()


def test_multi_tool_call_success():
    """Bounded, repeated (provider_call, agent_decision(tool_use), retrieval)
    cycles are the actual shape of a multi-turn tool loop — not a violation."""
    t = _rec()
    t.request(actor_role="patient")
    _tool_cycle(t, turn=1)
    _tool_cycle(t, turn=2)
    _tool_cycle(t, turn=3)
    _final_decision_through_display(t, turn=4)

    assert t.is_ordered()
    assert t.is_grounded()
    assert t.is_acceptable()


def test_provider_call_must_precede_every_decision():
    """An agent_decision can never legitimately appear without the
    provider_call whose response it was derived from — raw_bedrock.py's
    decision literally comes FROM `response.tool_calls`, so there is no path
    in the real loop where a decision has no preceding call. Skipping straight
    from request to a decision is rejected."""
    t = _rec()
    t.request(actor_role="patient")
    t.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")

    assert not t.is_ordered()


def test_missing_retrieval_after_a_tool_use_decision_is_rejected():
    """A decision to call a tool with no record of that tool ever running —
    the loop always executes the selected tool before calling the model
    again; skipping straight to the next provider_call is invalid."""
    t = _rec()
    t.request(actor_role="patient")
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")
    t.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")  # retrieval skipped

    assert not t.is_ordered()


def test_unfinished_tool_cycle_before_draft_is_rejected():
    """After a retrieval, the loop always calls the model again to learn
    whether another tool is needed or the answer is final — going straight
    from retrieval to draft skips that closing call+decision entirely."""
    t = _rec()
    t.request(actor_role="patient")
    _tool_cycle(t, turn=1)
    t.draft(draft_version=1, label=ProvenanceLabel.REAL, model_id="model-x",
            prompt_version="v3", citation_ids=["c1"])

    assert not t.is_ordered()


@pytest.mark.parametrize("next_event", ["provider_call", "retrieval"])
def test_final_answer_not_immediately_followed_by_draft_is_rejected(next_event):
    """A decision classified FINAL (no tool selected) must be followed by
    draft, not by more loop activity — either would mean the loop kept going
    after the model had already signalled it was done."""
    t = _rec()
    t.request(actor_role="patient")
    t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")
    t.agent_decision(tool_name=None, turn=1, stop_reason="end_turn")
    if next_event == "provider_call":
        t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")
    else:
        t.retrieval(document_count=1, citation_ids=["c1"], categories=["lab"])

    assert not t.is_ordered()


@pytest.mark.parametrize("loop_stage,call", [
    ("provider_call", lambda t: t.provider_call(label=ProvenanceLabel.REAL, model_id="model-x")),
    ("agent_decision", lambda t: t.agent_decision(tool_name="search_documents", turn=9, stop_reason="tool_use")),
    ("retrieval", lambda t: t.retrieval(document_count=1, citation_ids=["c1"], categories=["lab"])),
])
def test_post_draft_agent_activity_is_rejected(loop_stage, call):
    """Once `draft` has occurred, no further provider_call/agent_decision/
    retrieval may appear anywhere downstream — the drafted version must rest
    on evidence and decisions gathered strictly before it, not after."""
    t = _full(_rec())
    call(t)

    assert not t.is_ordered()
    assert not t.is_acceptable()


@pytest.mark.parametrize("repeated_stage,call", [
    ("validation", lambda t: t.validation(passed=True, validation_code="PASS", citation_ids=["c1"])),
    ("review", lambda t: t.review(decision="approved", draft_version=1)),
    ("display", lambda t: t.display(draft_version=1, label=ProvenanceLabel.REAL)),
])
def test_a_terminal_stage_repeating_is_rejected(repeated_stage, call):
    """draft/validation/review/display each occur exactly once — a repeat of
    any of them, even appended at the very end, is invalid."""
    t = _full(_rec())
    call(t)

    assert not t.is_ordered()
    assert not t.is_acceptable()


# --- is_grounded: ordering can be structurally correct with zero evidence --- #


def test_a_correctly_ordered_but_ungrounded_trace_is_not_acceptable():
    """A trace that goes straight from request to a FINAL decision — never
    calling a tool at all — is not malformed by is_ordered()'s state machine
    (nothing about its sequence is wrong), but it is not the real, evidence-
    grounded path the client requires. This is why is_grounded() is a
    separate check: "wrong shape" and "right shape, no evidence" are
    different failures with different fixes."""
    t = _rec()
    t.request(actor_role="patient")
    _final_decision_through_display(t, turn=1)

    assert t.is_ordered()
    assert not t.is_grounded()
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


def test_review_records_only_a_decision_category_and_version_never_who_decided():
    """review() no longer accepts decided_by_user_id — who decided is
    audit_logs' job, not this trace's."""
    t = _rec()
    t.review(decision="approved", draft_version=2)

    assert t.events[-1].attributes == {"decision": "approved", "draft_version": 2}
    with pytest.raises(TypeError):
        t.review(decision="approved", draft_version=2, decided_by_user_id=13)
    with pytest.raises(ForbiddenPayload):
        t.record(Stage.REVIEW, name="Dr. Grace Kim")
    with pytest.raises(ForbiddenPayload):
        t.record(Stage.REVIEW, decided_by_user_id=13)
    with pytest.raises(ForbiddenPayload):
        t.record(Stage.REVIEW, user_id=13)


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
    # 8 distinct stages, but provider_call and agent_decision each occur
    # twice in one tool cycle (see _full) — 10 events total.
    assert len(t.events) == 10


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
