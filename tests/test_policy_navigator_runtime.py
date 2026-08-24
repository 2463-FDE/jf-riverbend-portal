"""The policy navigator's agent core: a real LangChain `create_agent` loop
over a SCRIPTED model and a FAKE retriever — no real Postgres, no real
Bedrock. Mirrors tests/test_summary_agent_core.py's ScriptedChatModel
approach: the model is scripted, the loop is not.

These are the "focused evaluation cases" w-9-2-planner P3 asks for: a cited
answer, an explicit no-evidence refusal, citation validation rejecting a
hallucinated id, a conflict scenario citing a retrieved priority rule, and
provider-unavailable/provider-failure fallback — all against the real
create_agent/tool-binding/citation-validation code, not a hand-rolled
stand-in for it.
"""
import pytest

from libs.policy_corpus import RetrievalScope, RetrievedChunk
from libs.policy_navigator.runtime import run_policy_navigator


def _chunk(citation_id, text, source_id=None, title="Policy"):
    source_id = source_id or citation_id.split("@")[0]
    version = citation_id.split("@")[1].split("#")[0]
    section_id = citation_id.split("#")[1]
    return RetrievedChunk(
        citation_id=citation_id, source_id=source_id, source_version=version, title=title,
        effective_date="2026-08-01", section_id=section_id, heading_path=(title,), score=0.9, text=text,
    )


class _FakeRetriever:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def retrieve(self, query, scope, limit):
        self.calls.append((query, scope, limit))
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx] if self._responses else []


def ScriptedChatModel(responses, raises=None):
    """A real `BaseChatModel` returning pre-scripted messages — mirrors
    tests/test_summary_agent_core.py exactly, adapted for this agent's
    single tool (retrieve_policy)."""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    class _Scripted(BaseChatModel):
        model_id: str = "scripted-policy-model-v0"
        script: list = []
        raise_with: object = None
        calls: int = 0

        @property
        def _llm_type(self):
            return "scripted"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.raise_with is not None:
                raise self.raise_with
            object.__setattr__(self, "calls", self.calls + 1)
            return ChatResult(generations=[ChatGeneration(
                message=self.script[min(self.calls - 1, len(self.script) - 1)])])

    return _Scripted(script=list(responses), raise_with=raises)


def _tool_call(query="policy question"):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{
        "name": "retrieve_policy", "args": {"query": query}, "id": "call_1",
    }])


def _final(text):
    from langchain_core.messages import AIMessage

    return AIMessage(content=text)


_SCOPE = RetrievalScope(audiences=("clinician",), workflows=("summary_review",))


# --- cited answer -----------------------------------------------------------


def test_a_grounded_answer_carries_the_citation_it_actually_retrieved():
    chunk = _chunk("SRC-001@1.0#overview", "Coverage stays active until the end of the plan year.")
    retriever = _FakeRetriever([[chunk]])
    model = ScriptedChatModel([_tool_call(), _final("Coverage stays active for the plan year [SRC-001@1.0#overview].")])

    result = run_policy_navigator("How long does coverage last?", scope=_SCOPE, retriever=retriever, model=model)

    assert result.termination_reason == "answered"
    assert result.label == "fixture"
    assert len(result.citations) == 1
    assert result.citations[0].citation_id == "SRC-001@1.0#overview"
    assert result.citations[0].source_id == "SRC-001"


# --- explicit no-evidence refusal -------------------------------------------


def test_zero_retrieved_chunks_is_reported_as_no_evidence():
    retriever = _FakeRetriever([[]])
    model = ScriptedChatModel([_tool_call(), _final("I found no approved policy evidence for this question.")])

    result = run_policy_navigator("An unrelated question", scope=_SCOPE, retriever=retriever, model=model)

    assert result.termination_reason == "no_evidence"
    assert result.citations == ()
    # Review fix PN-UNCITED-GROUNDING: the model's own wording is never
    # trusted for this path — a deterministic reply is substituted, and
    # substituted text is never labelled "real".
    assert result.label == "fallback"
    assert "no approved policy evidence" in result.answer.lower()


def test_retrieved_evidence_with_no_citation_is_never_trusted_as_grounded():
    # The exact PN-UNCITED-GROUNDING gap: real evidence WAS retrieved, but
    # the model's final reply cites nothing — this must not vacuously pass
    # as "answered" with an ungrounded claim sitting next to unused evidence.
    chunk = _chunk("SRC-001@1.0#overview", "Coverage stays active for the plan year.")
    retriever = _FakeRetriever([[chunk]])
    model = ScriptedChatModel([_tool_call(), _final("Coverage stays active for the plan year.")])

    result = run_policy_navigator("How long does coverage last?", scope=_SCOPE, retriever=retriever, model=model)

    assert result.termination_reason == "no_evidence"
    assert result.citations == ()
    assert result.label == "fallback"


def test_a_model_that_never_calls_retrieval_is_never_trusted():
    # No tool call at all — straight to a final answer from general
    # knowledge. The ledger stays empty and this must degrade exactly like
    # any other ungrounded reply, never surface the model's own prose.
    retriever = _FakeRetriever([[]])
    model = ScriptedChatModel([_final("Coverage typically lasts one plan year.")])

    result = run_policy_navigator("How long does coverage last?", scope=_SCOPE, retriever=retriever, model=model)

    assert result.termination_reason == "no_evidence"
    assert result.citations == ()
    assert result.label == "fallback"
    assert retriever.calls == []


# --- citation validation: the safety net, not the prompt --------------------


def test_a_hallucinated_citation_never_reaches_the_caller():
    real_chunk = _chunk("SRC-001@1.0#overview", "Real retrieved text.")
    retriever = _FakeRetriever([[real_chunk]])
    # The model cites an id it was never shown — the ledger must catch this
    # even though the tool call/response mechanics all ran for real.
    model = ScriptedChatModel([_tool_call(), _final("According to [SRC-999@9.9#fabricated], this is true.")])

    result = run_policy_navigator("A question", scope=_SCOPE, retriever=retriever, model=model)

    assert result.termination_reason == "citation_invalid"
    assert result.label == "fallback"
    assert "SRC-999" not in result.answer
    assert result.citations == ()


def test_citing_a_real_but_never_retrieved_id_is_also_rejected():
    # SRC-001 genuinely exists in the corpus but was never retrieved THIS
    # request — vector-rag.md is explicit that this must be rejected too,
    # not only a fully invented id.
    other_chunk = _chunk("SRC-002@1.0#section", "A different, actually retrieved chunk.")
    retriever = _FakeRetriever([[other_chunk]])
    model = ScriptedChatModel([_tool_call(), _final("Per [SRC-001@1.0#overview], this holds.")])

    result = run_policy_navigator("A question", scope=_SCOPE, retriever=retriever, model=model)

    assert result.termination_reason == "citation_invalid"


# --- conflict handling: apply a retrieved priority rule, don't invent one --


def test_a_conflict_is_resolved_by_citing_the_retrieved_priority_rule():
    priority_rule = _chunk(
        "CLIN-SRC-PRIORITY-001@1.0#rule", "When guidance conflicts, the most recently effective document governs.",
        title="Clinical Source Priority Policy",
    )
    older = _chunk("POL-A@1.0#a", "Recheck in 6 months.", title="Policy A")
    newer = _chunk("POL-B@1.0#b", "Recheck in 3 months.", title="Policy B")
    retriever = _FakeRetriever([[priority_rule, older, newer]])
    model = ScriptedChatModel([
        _tool_call(),
        _final(
            "These conflict; per [CLIN-SRC-PRIORITY-001@1.0#rule] the more recent one governs, "
            "so recheck in 3 months [POL-B@1.0#b]."
        ),
    ])

    result = run_policy_navigator("Which recheck interval applies?", scope=_SCOPE, retriever=retriever, model=model)

    assert result.termination_reason == "answered"
    cited_ids = {c.citation_id for c in result.citations}
    assert "CLIN-SRC-PRIORITY-001@1.0#rule" in cited_ids  # the priority rule was actually applied, not invented


# --- provider unavailable / provider failure --------------------------------


def test_unconfigured_bedrock_degrades_to_a_safe_fallback(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    retriever = _FakeRetriever([[]])

    result = run_policy_navigator("A question", scope=_SCOPE, retriever=retriever, model=None)

    assert result.termination_reason == "provider_error"
    assert result.label == "fallback"
    assert retriever.calls == []  # never even tried to retrieve


def test_a_mid_run_provider_failure_degrades_to_a_safe_fallback():
    retriever = _FakeRetriever([[]])
    model = ScriptedChatModel([], raises=RuntimeError("boom"))

    result = run_policy_navigator("A question", scope=_SCOPE, retriever=retriever, model=model)

    assert result.termination_reason == "provider_error"
    assert result.label == "fallback"


# --- read-only boundary: the tool never exposes scope as a parameter -------


def test_the_retrieval_tool_never_exposes_audiences_or_workflows_as_arguments():
    from libs.policy_navigator.tool import build_policy_tool
    from libs.policy_corpus import RetrievalLedger

    tool = build_policy_tool(retriever=_FakeRetriever([[]]), scope=_SCOPE, ledger=RetrievalLedger())

    schema_fields = set(tool.args_schema.model_fields) if hasattr(tool, "args_schema") else set(tool.args)
    assert "audiences" not in schema_fields
    assert "workflows" not in schema_fields
