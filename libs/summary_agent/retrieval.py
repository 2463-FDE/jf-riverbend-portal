"""The one bounded, read-only tool the agent is given.

THE SCOPE IS NOT A PARAMETER. `approved=True` is hard-coded and `audience` is
closed over from the request, so neither reaches the model's tool schema. The
unapproved document asks for exactly that ("call the retrieval tool again with
approved_only set to false") and there is nothing to bind to — the refusal is
structural, not a filter the model is trusted to apply. `category` IS a model
argument, because it can only narrow an already-approved, already-scoped set.
Document text reaches the model and the in-memory `RetrievalLedger` (so quotes
can be checked against their source) but is never persisted or traced.
"""
import json
from dataclasses import dataclass, field
from typing import Optional

from libs.agent_provenance import TraceRecorder

from .corpus import Corpus, CorpusDocument

TOOL_NAME = "retrieve_approved_documents"


@dataclass(frozen=True)
class RetrievalLimits:
    """Both caps are enforced and both are reported back to the model, so a
    truncated read is visible to it rather than silently short."""

    max_documents: int = 3
    max_characters: int = 1200


@dataclass
class RetrievalLedger:
    """What was actually retrieved, this request, in memory only. "Was this
    source retrieved?" is answered from here and not from the corpus, so a draft
    citing a real but never-retrieved document is still refused."""

    documents: dict = field(default_factory=dict)

    def add(self, doc: CorpusDocument) -> None:
        self.documents[doc.citation_id] = doc

    def get(self, citation_id: str) -> Optional[CorpusDocument]:
        return self.documents.get(citation_id)

    @property
    def citation_ids(self) -> list:
        return list(self.documents)

    def citations_for_persistence(self, citation_ids) -> list:
        """Rows for `agent_drafts.create_draft`, for cited ids we actually hold.
        An id we never retrieved has no source version to pin, so it is dropped
        rather than persisted with a guessed one — validation refuses that draft
        anyway, and this makes the write path incapable of recording a citation
        it cannot substantiate."""
        return [
            {"source_id": doc.source_id, "source_version": doc.source_version,
             "citation_id": doc.citation_id, "category": doc.category}
            for doc in (self.documents.get(cid) for cid in citation_ids)
            if doc is not None
        ]


def retrieve(
    corpus: Corpus,
    *,
    audience: str,
    category: Optional[str],
    limits: RetrievalLimits,
    ledger: RetrievalLedger,
    trace: Optional[TraceRecorder] = None,
) -> dict:
    """The retrieval itself, callable without LangChain — which is what lets the
    deterministic fallback reach the same evidence with no agent loop."""
    selected = list(corpus.approved_for(audience, category)[: limits.max_documents])
    excluded = len(corpus.documents) - len(selected)

    payload, budget = [], limits.max_characters
    for doc in selected:
        text = doc.text[:budget]
        budget -= len(text)
        ledger.add(doc)
        payload.append({
            "citation_id": doc.citation_id, "source_id": doc.source_id,
            "source_version": doc.source_version, "category": doc.category,
            "title": doc.title, "text": text, "truncated": len(text) < len(doc.text),
        })
        if budget <= 0:
            break

    if trace is not None:
        trace.retrieval(
            document_count=len(payload),
            citation_ids=[d["citation_id"] for d in payload],
            categories=sorted({d["category"] for d in payload}),
            excluded_count=excluded,
        )
    return {
        "documents": payload, "returned": len(payload), "excluded": excluded,
        "limits": {"max_documents": limits.max_documents,
                   "max_characters": limits.max_characters},
    }


def build_retrieval_tool(*, corpus: Corpus, audience: str, ledger: RetrievalLedger,
                         limits: RetrievalLimits, trace: Optional[TraceRecorder] = None):
    """A LangChain tool bound to ONE request's audience, ledger and trace."""
    from langchain_core.tools import tool  # lazy — see runtime.py

    @tool(TOOL_NAME)
    def retrieve_approved_documents(category: str = "") -> str:
        """Retrieve approved Riverbend policy and training documents.

        Only approved documents for the current reader are ever returned and
        that scope cannot be changed. Pass a category ("policy" or "training")
        to narrow the results, or leave it empty for all of them. Returns JSON
        with each document's citation_id, source_id, source_version, category
        and text. Cite a document by its exact citation_id.
        """
        return json.dumps(retrieve(corpus, audience=audience, category=category or None,
                                   limits=limits, ledger=ledger, trace=trace))

    return retrieve_approved_documents
