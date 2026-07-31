"""Embed-once-and-cache pipeline: builds the capped corpus from deterministic
seed data, embeds any records not already in the persisted cache, and writes
the updated cache back to disk. A second run over unchanged records makes
zero embedding calls (see embedding_cache.py).

This pipeline does not run the retrieval-eval harness itself (recall@k,
precision@k, fragmentation metrics) — that is a separate, later commit. This
commit only builds the corpus and its cached embeddings.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

from libs.embedding_client import EmbeddingClient

from .config import CorpusConfig
from .corpus import CorpusRecord, build_corpus
from .embedding_cache import EmbeddingCache
from .vector_store import IndexResult, VectorStore, model_tag_for_provider


@dataclass
class PipelineResult:
    corpus: List[CorpusRecord]
    vectors_by_record_id: Dict[str, List[float]]
    cache_hits: int
    newly_embedded: int
    index_result: Optional[IndexResult] = None


def run_pipeline(
    config: Optional[CorpusConfig] = None,
    embedding_client: Optional[EmbeddingClient] = None,
    vector_store: Optional[VectorStore] = None,
) -> PipelineResult:
    config = config or CorpusConfig()
    embedding_client = embedding_client or EmbeddingClient()

    corpus = build_corpus(config.max_records)
    # model_tag_for_provider is the same function PgVectorStore uses to derive
    # its own persisted identity — reusing it here (rather than re-deriving
    # independently) is what keeps the disk cache and the DB row agreeing on
    # "which model made this vector" after a model swap (e.g. OLLAMA_EMBED_MODEL).
    cache = EmbeddingCache(
        cache_dir=config.cache_dir,
        provider=embedding_client.provider_name,
        model=model_tag_for_provider(embedding_client.provider_name),
    )

    vectors_by_record_id, cache_hits, newly_embedded = cache.get_or_embed_all(corpus, embedding_client)

    # Persisting to a vector_store (e.g. PgVectorStore) is opt-in: passing
    # none (the default) keeps this pipeline exactly as it was before Stage 2
    # — disk-cached vectors only, no database persistence.
    index_result = vector_store.index(corpus, vectors_by_record_id) if vector_store is not None else None

    return PipelineResult(
        corpus=corpus,
        vectors_by_record_id=vectors_by_record_id,
        cache_hits=cache_hits,
        newly_embedded=newly_embedded,
        index_result=index_result,
    )


if __name__ == "__main__":
    result = run_pipeline()
    print(
        f"corpus_size={len(result.corpus)} "
        f"cache_hits={result.cache_hits} newly_embedded={result.newly_embedded}"
    )
