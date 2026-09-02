"""The one bounded, read-only tool the policy navigator agent is given
(w-9-2-planner P3). Mirrors libs/summary_agent/retrieval.py's
build_retrieval_tool: SCOPE IS NOT A PARAMETER — `scope`'s audiences/
workflows are closed over from the request and never reach the model's tool
schema. `topic`, the one thing the model may pass, can only NARROW that
already-authorized set (vector-rag.md).
"""
import json
from dataclasses import replace
from typing import Optional

from libs.policy_corpus import RetrievalLedger, RetrievalScope
from libs.tracing.spans import safe_span

TOOL_NAME = "retrieve_policy"
_DEFAULT_LIMIT = 6
_TRACER_NAME = "policy_navigator"


def build_policy_tool(*, retriever, scope: RetrievalScope, ledger: RetrievalLedger, limit: int = _DEFAULT_LIMIT,
                      correlation_id: Optional[str] = None):
    """A LangChain tool bound to ONE request's scope, ledger, and retriever."""
    from langchain_core.tools import tool  # lazy — mirrors runtime.py

    @tool(TOOL_NAME)
    def retrieve_policy(query: str, topic: str = "") -> str:
        """Retrieve approved synthetic Riverbend policy text relevant to `query`.

        Only policy for the current caller's authorized audience/workflow
        scope is ever returned, and that scope cannot be changed. Pass
        `topic` to narrow within it, or leave it empty. Returns JSON with
        each chunk's citation_id, source_id, source_version, title,
        section_id, and text. Cite a chunk ONLY by its exact citation_id.
        """
        narrowed = replace(scope, topic=topic or None)
        # A real span only for a real retrieval attempt — W10 metrics
        # Stage 3: never fabricated for a stage that did not occur.
        attrs = {"correlation_id": correlation_id} if correlation_id else {}
        with safe_span(_TRACER_NAME, "policy_navigator.retrieval", attrs) as span:
            chunks = retriever.retrieve(query, narrowed, limit)
            span.set_attribute("document_count", len(chunks))
        ledger.record(chunks)
        return json.dumps(
            {
                "returned": len(chunks),
                "chunks": [
                    {
                        "citation_id": c.citation_id, "source_id": c.source_id,
                        "source_version": c.source_version, "title": c.title,
                        "section_id": c.section_id, "text": c.text,
                    }
                    for c in chunks
                ],
            }
        )

    return retrieve_policy
