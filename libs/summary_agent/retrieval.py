"""The one bounded, read-only tool the agent is given.

THE SCOPE IS NOT A PARAMETER. `scope`'s audiences/workflows are closed over
from the request and never reach the model's tool schema — the model may
only choose a search `query` and, optionally, narrow further with
`category` (mapped to `RetrievalScope.topic`). Document text reaches the
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
    category: Optional[str],
    limits: RetrievalLimits,
    ledger: RetrievalLedger,
    trace: Optional[TraceRecorder] = None,
) -> dict:
    """The retrieval itself, callable without LangChain — which is what lets the
    deterministic fallback reach the same evidence with no agent loop.

    `retriever=None` (retrieval infrastructure unavailable — see
    summary_agent_path._build_retriever) and any exception the retriever
    itself raises both degrade to zero chunks rather than propagating: a
    retrieval problem must never crash the fallback path, which has no
    other safety net once `_default_model()` has already failed."""
    narrowed = replace(scope, topic=category or None)
    chunks = []
    if retriever is not None:
        try:
            chunks = retriever.retrieve(query, narrowed, limits.max_documents)
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
            # A retrieved chunk carries no category of its own (unlike the
            # retired embedded manifest) — this records the caller-narrowed
            # topic filter itself, a bounded low-cardinality value.
            categories=(category,) if category else (),
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
    def retrieve_approved_documents(query: str, category: str = "") -> str:
        """Retrieve approved Riverbend policy and training documents relevant to `query`.

        Only approved documents for the current reader are ever returned and
        that scope cannot be changed. Pass a short search query describing
        what you need, and optionally a category ("policy" or "training") to
        narrow the results. Returns JSON with each document's citation_id,
        source_id, source_version, title, section_id and text. Cite a
        document by its exact citation_id.
        """
        return json.dumps(retrieve(retriever, scope=scope, query=query, category=category or None,
                                   limits=limits, ledger=ledger, trace=trace))

    return retrieve_approved_documents
