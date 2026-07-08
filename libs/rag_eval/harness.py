"""Retrieval-eval harness: runs `db/seed/goldset.json` questions against the
Commit 2 corpus/embedding-cache pipeline (`libs/rag_corpus`) and reports
recall@k, precision@k, and two fragmentation metrics (duplicate-rate,
fragment-coverage gap) that surface AUD-09's effect on retrieval quality.

Measurement only: does not implement retrieval matching for production use,
does not fix AUD-09, and does not send corpus/query text to a cloud
provider — it reuses `libs/embedding_client`, which offers no cloud provider
by design. See `adr/0004-master-patient-index-match-key.md` for the proposed
production fix.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from libs.embedding_client import EmbeddingClient
from libs.rag_corpus import CorpusConfig, CorpusRecord, run_pipeline

from .goldset import load_goldset
from .identity_proxy import cluster_patients
from .metrics import EvalReport, compute_metrics
from .similarity import cosine_similarity

_DEFAULT_GOLDSET_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "db", "seed", "goldset.json")


@dataclass
class EvalConfig:
    top_k: int = field(default_factory=lambda: int(os.getenv("RAG_EVAL_TOP_K", "1")))
    goldset_path: str = field(default_factory=lambda: os.getenv("RAG_EVAL_GOLDSET_PATH", _DEFAULT_GOLDSET_PATH))


def _retrieve_top_k(
    query_vector: List[float],
    corpus: List[CorpusRecord],
    vectors_by_record_id: Dict[str, List[float]],
    k: int,
) -> List[CorpusRecord]:
    scored = [
        (cosine_similarity(query_vector, vectors_by_record_id[record.record_id]), record) for record in corpus
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [record for _, record in scored[:k]]


def run_eval(
    eval_config: Optional[EvalConfig] = None,
    corpus_config: Optional[CorpusConfig] = None,
    embedding_client: Optional[EmbeddingClient] = None,
) -> EvalReport:
    eval_config = eval_config or EvalConfig()
    embedding_client = embedding_client or EmbeddingClient()

    pipeline_result = run_pipeline(config=corpus_config, embedding_client=embedding_client)
    gold_cases = load_goldset(eval_config.goldset_path)

    # Query embeddings are not cached — only the corpus is (see
    # libs/rag_corpus/embedding_cache.py). There are only a handful of
    # gold-set queries, so embedding them fresh each run is inexpensive and
    # keeps the cache's job (avoid re-embedding the corpus) unambiguous.
    query_vectors = embedding_client.embed([case.query for case in gold_cases])

    retrieved_by_case = {
        case.query: _retrieve_top_k(
            query_vector, pipeline_result.corpus, pipeline_result.vectors_by_record_id, eval_config.top_k
        )
        for case, query_vector in zip(gold_cases, query_vectors)
    }

    clusters = cluster_patients()

    return compute_metrics(
        gold_cases=gold_cases,
        corpus=pipeline_result.corpus,
        retrieved_by_case=retrieved_by_case,
        clusters=clusters,
        top_k=eval_config.top_k,
        provider_name=embedding_client.provider_name,
    )


if __name__ == "__main__":
    from .report import render_markdown

    print(render_markdown(run_eval()))
