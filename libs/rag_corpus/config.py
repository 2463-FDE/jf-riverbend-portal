"""Config for the RAG corpus builder and embedding-cache pipeline.

Config-only knobs (env vars) — no hardcoded corpus size or cache location.
"""
import os
from dataclasses import dataclass, field


@dataclass
class CorpusConfig:
    max_records: int = field(default_factory=lambda: int(os.getenv("RAG_CORPUS_MAX_RECORDS", "200")))
    cache_dir: str = field(default_factory=lambda: os.getenv("RAG_EMBEDDING_CACHE_DIR", ".cache/rag_embeddings"))


@dataclass
class VectorStoreConfig:
    """Selects and configures the retrieval store (vector_store.py). Reuses
    the same DB_* env vars every service already reads (see e.g.
    services/roi-service/config.py) — this is the existing Postgres, not a
    new data store."""

    store: str = field(default_factory=lambda: os.getenv("RAG_VECTOR_STORE", "memory"))
    db_host: str = field(default_factory=lambda: os.getenv("DB_HOST", "postgres"))
    db_port: str = field(default_factory=lambda: os.getenv("DB_PORT", "5432"))
    db_name: str = field(default_factory=lambda: os.getenv("DB_NAME", "riverbend"))
    db_user: str = field(default_factory=lambda: os.getenv("DB_USER", "riverbend_app"))
    db_password: str = field(default_factory=lambda: os.getenv("DB_PASSWORD", ""))
