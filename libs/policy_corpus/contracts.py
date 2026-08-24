"""Data contracts for the synthetic policy RAG corpus foundation
(w-9-2-planner P2): manifest-derived document/chunk metadata only. No
embedding, retrieval-scope, or citation-ledger concerns live here yet — see
.claude/skills/w-9-2-planner/references/vector-rag.md for those later slices.
"""
from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class ChunkingConfig:
    strategy: str
    max_characters: int
    overlap_characters: int
    minimum_characters: int
    preserve_heading_path: bool


@dataclass(frozen=True)
class IngestionConfig:
    content_root: str
    allowed_extensions: Tuple[str, ...]
    encoding: str
    max_document_bytes: int
    max_documents: int
    chunking: ChunkingConfig
    required_chunk_metadata: Tuple[str, ...]


@dataclass(frozen=True)
class PolicyDocumentMeta:
    """One manifest-declared document entry — metadata only, never document
    prose. The manifest is the sole metadata authority (vector-rag.md's
    ingestion contract): audiences/workflows/topics/allowed_uses/
    prohibited_uses/relationships all come from here, never inferred from
    the Markdown body."""

    source_id: str
    source_version: str
    effective_date: str
    title: str
    owner: str
    approval_status: str
    synthetic: bool
    retrieval_enabled: bool
    content_path: str
    content_sha256: str
    audiences: Tuple[str, ...]
    workflows: Tuple[str, ...]
    topics: Tuple[str, ...]
    allowed_uses: Tuple[str, ...]
    prohibited_uses: Tuple[str, ...]
    relationships: Tuple[dict, ...] = field(default_factory=tuple)

    @property
    def citation_id(self) -> str:
        return f"{self.source_id}@{self.source_version}"

    @property
    def is_ingestable(self) -> bool:
        """The manifest's own gate: only a document that is synthetic,
        retrieval-enabled, and approved for training may ever be ingested —
        always these three explicit flags, never inferred. A document
        failing this is administratively excluded, not a manifest defect —
        see manifest.py's module docstring for why that is not a hard
        validation error."""
        return self.synthetic and self.retrieval_enabled and self.approval_status == "approved_training"


@dataclass(frozen=True)
class PolicyManifest:
    schema_version: int
    corpus_id: str
    notice: str
    ingestion: IngestionConfig
    documents: Tuple[PolicyDocumentMeta, ...]


@dataclass(frozen=True)
class PolicyChunk:
    """One deterministically-chunked section of an ingestable document,
    ready to persist as a policy_chunks row
    (db/migrations/024_policy_corpus.sql). Carries only the document's
    identity, never its full metadata — that lives on the policy_documents
    row instead."""

    chunk_id: str  # stable: source_id@source_version#section_id
    source_id: str
    source_version: str
    section_id: str
    heading_path: Tuple[str, ...]
    text: str
    chunk_hash: str

    @property
    def char_count(self) -> int:
        return len(self.text)
