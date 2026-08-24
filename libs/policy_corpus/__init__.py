"""Synthetic policy RAG corpus (w-9-2-planner P2): manifest validation,
section chunking, a real (Bedrock) embedding provider, dimension-enforced
idempotent persistence, and metadata-filtered pgvector retrieval with a
citation ledger, for docs/RagDocs. No graph store or policy-navigator agent
wiring yet — see references/vector-rag.md and migrations 024/025.
"""
from .bedrock_embedding_provider import BedrockPolicyEmbeddingProvider
from .chunking import chunk_markdown
from .contracts import ChunkingConfig, IngestionConfig, PolicyChunk, PolicyDocumentMeta, PolicyManifest
from .manifest import ManifestValidationError, load_ingestable_documents, load_manifest
from .persistence import (
    DimensionMismatchError,
    IngestionReport,
    existing_embedding_hashes,
    ingest_corpus,
    upsert_chunks,
    upsert_document,
    upsert_embedding,
)
from .retrieval import PolicyRetriever, RetrievalLedger, RetrievalScope, RetrievedChunk

__all__ = [
    "chunk_markdown", "ChunkingConfig", "IngestionConfig", "PolicyChunk", "PolicyDocumentMeta", "PolicyManifest",
    "ManifestValidationError", "load_ingestable_documents", "load_manifest", "BedrockPolicyEmbeddingProvider",
    "DimensionMismatchError", "IngestionReport", "existing_embedding_hashes", "ingest_corpus", "upsert_chunks",
    "upsert_document", "upsert_embedding", "PolicyRetriever", "RetrievalLedger", "RetrievalScope", "RetrievedChunk",
]
