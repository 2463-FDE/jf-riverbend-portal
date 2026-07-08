"""Deterministic corpus builder + embed-once-and-cache pipeline for the
Week 2 retrieval-eval harness. Reads only checked-in seed fixtures — never
the client's raw export, never a live database — and embeds through
libs/embedding_client, which offers no cloud provider by design.
"""
from .config import CorpusConfig
from .corpus import CorpusRecord, build_corpus
from .embedding_cache import EmbeddingCache
from .pipeline import PipelineResult, run_pipeline

__all__ = [
    "CorpusConfig",
    "CorpusRecord",
    "build_corpus",
    "EmbeddingCache",
    "PipelineResult",
    "run_pipeline",
]
