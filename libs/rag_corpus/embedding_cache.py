"""Persisted embedding cache — the "embed once, cache to disk" half of the
corpus pipeline. Keyed by a hash of each record's text, so a second run over
unchanged text makes zero embedding calls, and a record whose text actually
changed is correctly re-embedded (its hash changes). This is the specific
cost failure mode (re-embedding the same corpus on every eval run) this
deliverable exists to avoid.

The cache file is namespaced by provider AND model (see `model` below) — a
provider tag alone (e.g. "ollama") does not say which model produced a
vector, so changing OLLAMA_EMBED_MODEL while keeping the same provider would
otherwise still hit this cache under unchanged content_hash and hand back the
OLD model's vector. `libs/rag_corpus/vector_store.py::model_tag_for_provider`
is the single source of truth for that model tag — pass its output here as
`model` so this cache and PgVectorStore's persisted identity always agree.

Cache files live under RAG_EMBEDDING_CACHE_DIR (default
.cache/rag_embeddings, gitignored) — never committed, since they're derived
data, not source. Never logs record text or vector contents, only counts.
"""
import hashlib
import json
import os
from typing import Dict, List, Tuple

from libs.embedding_client import EmbeddingClient
from libs.safe_logging import get_safe_logger

from .corpus import CorpusRecord

log = get_safe_logger(__name__)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_filename(provider: str, model: str) -> str:
    return f"{provider}.json" if not model else f"{provider}__{model}.json"


class EmbeddingCache:
    def __init__(self, cache_dir: str, provider: str, model: str = ""):
        self._provider = provider
        self._model = model
        self._path = os.path.join(cache_dir, _cache_filename(provider, model))
        self._data: Dict[str, List[float]] = self._load()

    def _load(self) -> Dict[str, List[float]]:
        if not os.path.exists(self._path):
            return {}
        with open(self._path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh)
        os.replace(tmp_path, self._path)

    def get_or_embed_all(
        self, records: List[CorpusRecord], embedding_client: EmbeddingClient
    ) -> Tuple[Dict[str, List[float]], int, int]:
        keys_by_record_id = {record.record_id: _content_hash(record.text) for record in records}

        misses = [record for record in records if keys_by_record_id[record.record_id] not in self._data]
        cache_hits = len(records) - len(misses)

        if misses:
            vectors = embedding_client.embed([record.text for record in misses])
            for record, vector in zip(misses, vectors):
                self._data[keys_by_record_id[record.record_id]] = vector
            self._save()

        log.info(
            "embedding_cache resolved corpus (provider=%s, model=%s, total=%s, cache_hits=%s, newly_embedded=%s)",
            self._provider,
            self._model,
            len(records),
            cache_hits,
            len(misses),
        )

        vectors_by_record_id = {
            record.record_id: self._data[keys_by_record_id[record.record_id]] for record in records
        }
        return vectors_by_record_id, cache_hits, len(misses)
