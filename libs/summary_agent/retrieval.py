"""The one bounded, read-only tool the agent is given.

THE SCOPE IS NOT A PARAMETER, AND NEITHER IS ITS TOPIC. `scope` — audiences,
workflows, and any topic narrowing within them — is closed over from the
trusted caller and passed to `PolicyRetriever` completely unchanged; nothing
the model supplies ever reaches it. The model may only choose a free-text
search `query`; the vector search itself already ranks relevant approved
chunks, and review fix SA-TOPIC-MISMATCH removed the one-time `category`
tool argument this module used to map onto `RetrievalScope.topic` — corpus
topics are manifest-defined and may change, and an invalid model-selected
one could only ever suppress valid evidence, never add any (a false
no-evidence refusal, not a widened scope, but still not worth the risk for
a control that added no real narrowing PolicyRetriever's own semantic
ranking didn't already do). `PolicyRetriever` itself keeps a general
optional topic capability — this module simply never exercises it; other
trusted callers may still pass a real manifest topic like
`laboratory_results` through `scope` directly. Document text reaches the
model and the in-memory `RetrievalLedger` (so quotes can be checked against
their source) but is never persisted or traced.

W10 Final Stage 5: retrieval is backed by the same pgvector-approved policy
corpus and `PolicyRetriever` `libs/policy_navigator` already uses, replacing
the embedded four-document manifest (`corpus.py`/`manifest.json`, kept only
as a fixture-labelled test resource — see that module's own docstring).
"""
import json
from dataclasses import dataclass, replace
from typing import Optional

from libs.agent_provenance import TraceRecorder
from libs.policy_corpus import RetrievalLedger, RetrievalScope
from libs.safe_logging import get_safe_logger

log = get_safe_logger(__name__)

TOOL_NAME = "retrieve_approved_documents"


@dataclass(frozen=True)
class RetrievalLimits:
    """Both caps are enforced and both are reported back to the model, so a
    truncated read is visible to it rather than silently short."""

    max_documents: int = 3
    max_characters: int = 1200


def citations_for_persistence(ledger: RetrievalLedger, citation_ids) -> list:
    """Rows for `agent_drafts.create_draft`, for cited ids we actually hold.
    An id we never retrieved has no source version to pin, so it is dropped
    rather than persisted with a guessed one — validation refuses that draft
    anyway, and this makes the write path incapable of recording a citation
    it cannot substantiate."""
    return [
        {"source_id": chunk.source_id, "source_version": chunk.source_version, "citation_id": chunk.citation_id}
        for chunk in (ledger.get(cid) for cid in citation_ids)
        if chunk is not None
    ]


def retrieve(
    retriever,
    *,
    scope: RetrievalScope,
    query: str,
    limits: RetrievalLimits,
    ledger: RetrievalLedger,
    trace: Optional[TraceRecorder] = None,
) -> dict:
    """The retrieval itself, callable without LangChain — which is what lets the
    deterministic fallback reach the same evidence with no agent loop.

    `scope` reaches `PolicyRetriever` completely unchanged — review fix
    SA-TOPIC-MISMATCH: this module no longer narrows it by any model
    argument (see module docstring for why).

    `retriever=None` (retrieval infrastructure unavailable — see
    summary_agent_path._build_retriever) and any exception the retriever
    itself raises both degrade to zero chunks rather than propagating: a
    retrieval problem must never crash the fallback path, which has no
    other safety net once `_default_model()` has already failed."""
    chunks = []
    if retriever is not None:
        try:
            chunks = retriever.retrieve(query, scope, limits.max_documents)
        except Exception as exc:
            log.warning("summary agent retrieval failed (error_type=%s)", type(exc).__name__)

    payload, kept, budget = [], [], limits.max_characters
    for chunk in chunks:
        text = chunk.text[:budget]
        budget -= len(text)
        # The TRUNCATED text, not the chunk. The ledger is what validation
        # checks quotes against, so holding the full text here would let a
        # draft quote past the character cap — words the model was never shown.
        truncated = replace(chunk, text=text)
        kept.append(truncated)
        payload.append({
            "citation_id": truncated.citation_id, "source_id": truncated.source_id,
            "source_version": truncated.source_version, "title": truncated.title,
            "section_id": truncated.section_id, "text": text, "truncated": len(text) < len(chunk.text),
        })
        if budget <= 0:
            break
    ledger.record(kept)
    excluded = len(chunks) - len(payload)

    if trace is not None:
        trace.retrieval(
            document_count=len(payload),
            citation_ids=[d["citation_id"] for d in payload],
            # A retrieved chunk carries no category of its own, and
            # SA-TOPIC-MISMATCH removed the only other source of one.
            categories=(),
            excluded_count=excluded,
        )
    return {
        "documents": payload, "returned": len(payload), "excluded": excluded,
        "limits": {"max_documents": limits.max_documents, "max_characters": limits.max_characters},
    }


def build_retrieval_tool(*, retriever, scope: RetrievalScope, ledger: RetrievalLedger,
                         limits: RetrievalLimits, trace: Optional[TraceRecorder] = None):
    """A LangChain tool bound to ONE request's scope, ledger, retriever and trace."""
    from langchain_core.tools import tool  # lazy — see runtime.py

    @tool(TOOL_NAME)
    def retrieve_approved_documents(query: str) -> str:
        """Retrieve approved Riverbend documents relevant to `query`.

        Only approved documents for the current reader are ever returned and
        that scope cannot be changed. Pass a short search query describing
        what you need. Returns JSON with each document's citation_id,
        source_id, source_version, title, section_id and text. Cite a
        document by its exact citation_id.
        """
        return json.dumps(retrieve(retriever, scope=scope, query=query, limits=limits, ledger=ledger, trace=trace))

    return retrieve_approved_documents
