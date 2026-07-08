"""Config for the RAG corpus builder and embedding-cache pipeline.

Config-only knobs (env vars) — no hardcoded corpus size or cache location.
"""
import os
from dataclasses import dataclass, field


@dataclass
class CorpusConfig:
    max_records: int = field(default_factory=lambda: int(os.getenv("RAG_CORPUS_MAX_RECORDS", "200")))
    cache_dir: str = field(default_factory=lambda: os.getenv("RAG_EMBEDDING_CACHE_DIR", ".cache/rag_embeddings"))
