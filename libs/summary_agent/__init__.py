"""Week 8 patient-summary agent: a synthetic corpus, one bounded retrieval tool,
a LangChain v1 loop over Bedrock, and deterministic validation before anything
is persisted. Nothing here imports LangChain at module scope — see `runtime.py`.
"""
from .contracts import AgentRunResult, ComputationClaim, DraftParseError, QuoteClaim, StructuredDraft, parse_draft
from .corpus import Corpus, CorpusDocument, load_corpus
from .retrieval import (
    TOOL_NAME, RetrievalLedger, RetrievalLimits, build_retrieval_tool, citations_for_persistence, retrieve,
)
from .validation import ValidationOutcome, validate_draft
