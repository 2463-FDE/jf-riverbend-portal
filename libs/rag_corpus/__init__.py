"""Deterministic corpus builder + embed-once-and-cache pipeline for the
Week 2 retrieval-eval harness. Reads only checked-in seed fixtures — never
the client's raw export, never a live database — and embeds through
libs/embedding_client, which offers no cloud provider by design.
"""
from .config import CorpusConfig, VectorStoreConfig
from .corpus import CorpusRecord, build_corpus
from .embedding_cache import EmbeddingCache
from .pipeline import PipelineResult, run_pipeline
from .vector_store import (
    IndexResult,
    InMemoryCosineStore,
    PgVectorStore,
    VectorStore,
    build_vector_store,
    model_tag_for_provider,
)

__all__ = [
    "CorpusConfig",
    "CorpusRecord",
    "build_corpus",
    "EmbeddingCache",
    "PipelineResult",
    "run_pipeline",
    "VectorStoreConfig",
    "VectorStore",
    "InMemoryCosineStore",
    "PgVectorStore",
    "IndexResult",
    "build_vector_store",
    "model_tag_for_provider",
]
