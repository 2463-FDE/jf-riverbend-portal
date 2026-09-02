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
from typing import Optional

from libs.embedding_client import EmbeddingClient
from libs.rag_corpus import CorpusConfig, VectorStore, build_vector_store, run_pipeline

from .goldset import load_goldset
from .identity_proxy import cluster_patients
from .metrics import EvalReport, compute_metrics

_DEFAULT_GOLDSET_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "db", "seed", "goldset.json")


@dataclass
class EvalConfig:
    top_k: int = field(default_factory=lambda: int(os.getenv("RAG_EVAL_TOP_K", "1")))
    goldset_path: str = field(default_factory=lambda: os.getenv("RAG_EVAL_GOLDSET_PATH", _DEFAULT_GOLDSET_PATH))


def run_eval(
    eval_config: Optional[EvalConfig] = None,
    corpus_config: Optional[CorpusConfig] = None,
    embedding_client: Optional[EmbeddingClient] = None,
    vector_store: Optional[VectorStore] = None,
) -> EvalReport:
    eval_config = eval_config or EvalConfig()
    embedding_client = embedding_client or EmbeddingClient()
    # RAG_VECTOR_STORE=memory|pgvector (default memory) — see
    # libs/rag_corpus/vector_store.py. Retrieval here is intentionally
    # corpus-wide (no patient_id scope): this harness measures AUD-09
    # identity-fragmentation effects on retrieval quality across the whole
    # corpus, which requires being able to retrieve a duplicate patient's
    # record for another duplicate's query. The patient-scope filter itself
    # is exercised directly in tests/test_rag_vector_store.py and
    # tests/integration/test_pgvector_retrieval.py, not here.
    #
    # `provider` must come from the ACTUAL embedding_client, not
    # PgVectorStore's own default ("fake") — otherwise a real
    # EMBEDDING_PROVIDER=ollama run would persist/query vectors mislabeled as
    # "fake", or fail with a dimension mismatch attributed to the wrong
    # provider. `model` and `dimension` are not threaded through here: the
    # rag_embeddings column is a fixed vector(16) sized to
    # FakeEmbeddingProvider (see migration 010 / adr/0006's revisit trigger),
    # so a non-fake provider whose output dimension differs still fails —
    # correctly and loudly, via PgVectorStore's own dimension check — rather
    # than silently under a mislabeled identity.
    store = vector_store or build_vector_store(provider=embedding_client.provider_name)

    pipeline_result = run_pipeline(config=corpus_config, embedding_client=embedding_client, vector_store=store)
    gold_cases = load_goldset(eval_config.goldset_path)

    # Query embeddings are not cached — only the corpus is (see
    # libs/rag_corpus/embedding_cache.py). There are only a handful of
    # gold-set queries, so embedding them fresh each run is inexpensive and
    # keeps the cache's job (avoid re-embedding the corpus) unambiguous.
    query_vectors = embedding_client.embed([case.query for case in gold_cases])

    retrieved_by_case = {
        case.query: store.retrieve_top_k(query_vector, eval_config.top_k)
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
    # W10 Metrics Stage 5: --publish pushes this SAME run's sanitized,
    # numeric aggregate to a local Prometheus Pushgateway — see
    # libs/rag_eval_metrics. Opt-in and strictly additive: without the
    # flag, or with RAG_EVAL_PUSHGATEWAY_URL unset, behavior is unchanged.
    import argparse

    from libs.rag_eval_metrics import patient_record_corpus_gauges, push_metrics

    from .report import render_markdown

    _parser = argparse.ArgumentParser(description=__doc__)
    _parser.add_argument(
        "--publish", action="store_true", help="also push sanitized aggregate metrics to RAG_EVAL_PUSHGATEWAY_URL",
    )
    opts = _parser.parse_args()

    client = EmbeddingClient()
    report = run_eval(embedding_client=client)
    print(render_markdown(report))
    if opts.publish:
        published = push_metrics(
            pushgateway_url=os.getenv("RAG_EVAL_PUSHGATEWAY_URL", ""), corpus="patient_record_corpus",
            gauges=patient_record_corpus_gauges(report=report, embedding_client=client),
        )
        print(f"published={published}")
