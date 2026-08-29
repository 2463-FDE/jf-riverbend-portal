"""The policy navigator agent loop: LangChain v1 `create_agent` over Bedrock
Converse (w-9-2-planner P3). Mirrors libs/summary_agent/runtime.py's shape
(lazy langchain imports, real/fixture/fallback provenance, never raises for
a provider problem) but is stateless — no draft, no review gate, nothing
persisted. Read-only: it can only explain approved synthetic policy, never
book/cancel, change eligibility, approve summaries, release records, send
messages, or modify accounts (agents.md).

CITATION VALIDATION IS THE SAFETY NET, NOT THE PROMPT. The system prompt
asks the model to cite only what retrieve_policy actually returned, but
nothing here trusts that request — every `[citation_id]` token in the final
answer is checked against this run's OWN RetrievalLedger, and a single
invalid one discards the entire answer in favor of a safe, generic reply
(mirrors summary_agent's validate_draft gating a draft's acceptance, not
just advising on it).
"""
import os
import re
from typing import Optional

from libs.agent_provenance import ProvenanceLabel
from libs.deid import scrub
from libs.metrics import record_counter
from libs.policy_corpus import PolicyRetriever, RetrievalLedger, RetrievalScope
from libs.safe_logging import get_safe_logger

from .contracts import CitedSource, PolicyNavigatorResult
from .tool import build_policy_tool

log = get_safe_logger(__name__)

PROMPT_VERSION = "policy-navigator-v1"
DEFAULT_MAX_TURNS = 4

_SAFE_PROVIDER_REPLY = (
    "I couldn't reach the policy navigator just now. Please try again in a moment."
)
_SAFE_NO_EVIDENCE_REPLY = (
    "I found no approved policy evidence for this question within your authorized scope."
)
_SAFE_CITATION_INVALID_REPLY = (
    "I can't show that answer safely — it referenced policy text that wasn't actually retrieved "
    "for this question. Please try rephrasing your question."
)

_CITATION_RE = re.compile(r"\[([A-Za-z0-9_-]+@[0-9][0-9.]*#[a-z0-9][a-z0-9-]*)\]")

SYSTEM_PROMPT = """You explain how approved synthetic Riverbend Community Health policies say a
workflow is SUPPOSED to operate. You are strictly READ-ONLY: you cannot book
or cancel appointments, change eligibility, approve summaries, release
records, send messages, or modify accounts. If asked to do any of those,
say plainly that you cannot and that this tool only explains policy.

Use the retrieve_policy tool for evidence. The caller's audience and
workflow scope are fixed and cannot be changed; you may only choose the
search query and, optionally, narrow by topic.

Cite every factual claim with the exact citation_id retrieve_policy gave
you, in square brackets, e.g. [SRC-001@1.0#section-name]. Never write a
citation_id you were not shown by the tool, and never invent one.

If retrieve_policy returns zero relevant chunks after your attempts, say
plainly that you found no approved policy evidence for this question within
your authorized scope, and stop there — never answer from general knowledge.

If retrieved chunks conflict, look for a retrieved chunk that itself states
a source-priority or conflict-resolution rule, and apply exactly what it
says. Never decide priority yourself without such a retrieved rule; if none
was retrieved, say the conflict cannot be resolved from the available
evidence.

A policy document describes approved intent for how a workflow is supposed
to work. It never proves the running application actually behaves that way
— never claim that it does."""


class ProviderNotConfigured(RuntimeError):
    """No usable Bedrock model id. Raised before any network call."""


def _default_model():
    from langchain_aws import ChatBedrockConverse  # lazy — mirrors summary_agent/runtime.py

    model_id = os.getenv("BEDROCK_MODEL_ID", "")
    if not model_id or model_id == "changeme":
        raise ProviderNotConfigured("BEDROCK_MODEL_ID is not configured")
    return ChatBedrockConverse(model=model_id, region_name=os.getenv("AWS_REGION"))


def _model_id_of(model) -> Optional[str]:
    for attr in ("model_id", "model"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _citations_from(ledger: RetrievalLedger, citation_ids) -> tuple:
    seen, ordered = set(), []
    for citation_id in citation_ids:
        if citation_id in seen:
            continue
        seen.add(citation_id)
        chunk = ledger.get(citation_id)
        if chunk is not None:
            ordered.append(
                CitedSource(
                    citation_id=chunk.citation_id, source_id=chunk.source_id,
                    source_version=chunk.source_version, title=chunk.title, section_id=chunk.section_id,
                )
            )
    return tuple(ordered)


def run_policy_navigator(
    question: str,
    *,
    scope: RetrievalScope,
    retriever: PolicyRetriever,
    model=None,
    label: Optional[ProvenanceLabel] = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> PolicyNavigatorResult:
    """One bounded, stateless navigator turn. Never raises for a provider or
    citation problem — always returns a safe PolicyNavigatorResult.

    `model=None` builds the real ChatBedrockConverse and the run is labelled
    `real`. An injected model is labelled `fixture` unless the caller says
    otherwise, so a scripted test model can never be recorded as real.

    Emits the `policy_navigator_termination_total` golden-signal counter
    exactly once per call, labelled by the outcome this function is about to
    return — see docs/planning/policy-navigator-golden-signals-week7-08-25-2026.md.
    """
    result = _run_policy_navigator(
        question, scope=scope, retriever=retriever, model=model, label=label, max_turns=max_turns,
    )
    record_counter(
        "policy_navigator_termination_total",
        termination_reason=result.termination_reason,
        provenance_label=result.label,
    )
    return result


def _run_policy_navigator(
    question: str,
    *,
    scope: RetrievalScope,
    retriever: PolicyRetriever,
    model=None,
    label: Optional[ProvenanceLabel] = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> PolicyNavigatorResult:
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage

    # W10 Final Stage 3: scrub the caller's raw question once, before it
    # reaches either provider call this run makes — the chat model (via the
    # HumanMessage below) AND, transitively, the Titan embedding call inside
    # retrieve_policy (build_policy_tool), since the model can only ever
    # construct a tool-call query from what it was shown in this prompt.
    # Mirrors libs/eligibility_agent/runtimes/raw_bedrock.py's existing
    # scrub of the caller's chat message — same helper, same fail-closed
    # posture: a scrub failure must never fall back to the unscrubbed
    # original.
    try:
        question, deid_report = scrub(question)
    except Exception as exc:
        log.warning("policy navigator question scrub failed, refusing provider call (error_type=%s)", type(exc).__name__)
        return PolicyNavigatorResult(
            answer=_SAFE_PROVIDER_REPLY, citations=(), label=ProvenanceLabel.FALLBACK.value,
            model_id=None, termination_reason="provider_error",
        )
    if deid_report:
        # Categories/counts only, per DeidReport's own contract — never the
        # removed values, never the question itself.
        log.info("policy navigator question scrubbed before provider call (%s)", deid_report.summary())

    ledger = RetrievalLedger()

    if model is None:
        resolved = label or ProvenanceLabel.REAL
        try:
            model = _default_model()
        except Exception as exc:
            log.warning("policy navigator provider unavailable (error_type=%s)", type(exc).__name__)
            return PolicyNavigatorResult(
                answer=_SAFE_PROVIDER_REPLY, citations=(), label=ProvenanceLabel.FALLBACK.value,
                model_id=None, termination_reason="provider_error",
            )
    else:
        resolved = label or ProvenanceLabel.FIXTURE

    model_id = _model_id_of(model)
    policy_tool = build_policy_tool(retriever=retriever, scope=scope, ledger=ledger)
    agent = create_agent(model, [policy_tool], system_prompt=SYSTEM_PROMPT)

    try:
        state = agent.invoke(
            {"messages": [HumanMessage(content=question)]},
            config={"recursion_limit": 2 * max_turns + 1},
        )
        final = next(m for m in reversed(state["messages"]) if isinstance(m, AIMessage))
        answer_text = final.content if isinstance(final.content, str) else ""
    except Exception as exc:
        log.warning("policy navigator run failed (error_type=%s)", type(exc).__name__)
        return PolicyNavigatorResult(
            answer=_SAFE_PROVIDER_REPLY, citations=(), label=ProvenanceLabel.FALLBACK.value,
            model_id=model_id, termination_reason="provider_error",
        )

    cited_ids = _CITATION_RE.findall(answer_text)
    if any(not ledger.is_valid_citation(cid) for cid in cited_ids):
        log.warning("policy navigator cited an id never retrieved for this request")
        return PolicyNavigatorResult(
            answer=_SAFE_CITATION_INVALID_REPLY, citations=(), label=ProvenanceLabel.FALLBACK.value,
            model_id=model_id, termination_reason="citation_invalid",
        )

    if not cited_ids:
        # Review fix PN-UNCITED-GROUNDING: "answered" must never be reachable
        # without at least one retrieved, valid citation. Previously, a reply
        # with zero bracketed citations vacuously passed the check above and
        # was labelled "answered" whenever the ledger was non-empty (an
        # ungrounded claim next to real retrieved evidence it never actually
        # cited) — or, when the ledger WAS empty, the model's own raw prose
        # was trusted verbatim as the "no_evidence" reply instead of a
        # verified refusal. Both cases now get the SAME deterministic
        # substitution, and — per ProvenanceLabel's own contract — text this
        # module wrote, not the model, is never labelled "real".
        log.info("policy navigator produced no grounded citation; returning a safe refusal")
        return PolicyNavigatorResult(
            answer=_SAFE_NO_EVIDENCE_REPLY, citations=(), label=ProvenanceLabel.FALLBACK.value,
            model_id=model_id, termination_reason="no_evidence",
        )

    return PolicyNavigatorResult(
        answer=answer_text, citations=_citations_from(ledger, cited_ids), label=resolved.value,
        model_id=model_id, termination_reason="answered",
    )
