"""Synthetic policy RAG corpus foundation (w-9-2-planner P2): manifest
validation and deterministic section chunking for docs/RagDocs. No
embedding, retrieval, or persistence logic lives here yet — see
.claude/skills/w-9-2-planner/references/vector-rag.md for those later
slices, and db/migrations/024_policy_corpus.sql for the schema this
metadata is shaped to fit.
"""
from .chunking import chunk_markdown
from .contracts import ChunkingConfig, IngestionConfig, PolicyChunk, PolicyDocumentMeta, PolicyManifest
from .manifest import ManifestValidationError, load_ingestable_documents, load_manifest

__all__ = [
    "chunk_markdown",
    "ChunkingConfig",
    "IngestionConfig",
    "PolicyChunk",
    "PolicyDocumentMeta",
    "PolicyManifest",
    "ManifestValidationError",
    "load_ingestable_documents",
    "load_manifest",
]
