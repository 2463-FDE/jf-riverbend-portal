"""
Integration test for Stage 2's pgvector persistence + retrieval path
(libs/rag_corpus/vector_store.py::PgVectorStore) — requires the full stack up
(`make up`, on a *fresh* volume so db/schema.sql's rag_embeddings table and
`CREATE EXTENSION vector` are actually present; docker-compose.yml's postgres
image is pgvector/pgvector:0.8.0-pg15 specifically so that extension exists).

Connects to Postgres directly (not through a service, and not through the
gateway/records IDOR path — this deliverable adds no HTTP route) to drive
PgVectorStore against a real pgvector installation, proving what
tests/test_rag_vector_store.py's fake connection cannot: a genuine ANN query
and a genuine empty cross-patient result.

Requires the optional libs/rag_corpus/requirements.txt (pgvector, psycopg2)
installed; skipped, not failed, if they aren't.

Run with:  pytest -m integration tests/integration/test_pgvector_retrieval.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("pgvector")

from libs.embedding_client import EmbeddingClient, EmbeddingConfig  # noqa: E402
from libs.embedding_client.providers.fake_provider import FakeEmbeddingProvider  # noqa: E402
from libs.rag_corpus import CorpusConfig, InMemoryCosineStore, PgVectorStore, VectorStoreConfig  # noqa: E402
from libs.rag_corpus.pipeline import run_pipeline  # noqa: E402

pytestmark = pytest.mark.integration

_PROVIDER = "fake"
_DIMENSION = 16  # matches FakeEmbeddingProvider and db/migrations/010_pgvector_embeddings.sql's vector(16)

_DB_CONFIG = VectorStoreConfig(
    store="pgvector",
    # DB_HOST/etc default to "postgres" (the in-network hostname) everywhere
    # else in this repo; this test runs on the host, so default to localhost
    # instead — mirrors GATEWAY_URL's localhost default in the other
    # integration tests. docker-compose.yml publishes 5432 to the host.
    db_host=os.getenv("DB_HOST", "localhost"),
    db_port=os.getenv("DB_PORT", "5432"),
    db_name=os.getenv("DB_NAME", "riverbend"),
    db_user=os.getenv("DB_USER", "riverbend_app"),
    db_password=os.getenv("DB_PASSWORD", "changeme"),
)


@pytest.fixture(autouse=True)
def _clean_rag_embeddings():
    # Deterministic corpus + FakeEmbeddingProvider means every test in this
    # file computes the same record_ids/content_hashes/vectors — clear this
    # provider's rows first so each test's "first" index() call is genuinely
    # first, not a no-op skip left over from a previous test.
    conn = psycopg2.connect(
        host=_DB_CONFIG.db_host,
        port=_DB_CONFIG.db_port,
        dbname=_DB_CONFIG.db_name,
        user=_DB_CONFIG.db_user,
        password=_DB_CONFIG.db_password,
    )
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rag_embeddings WHERE provider = %s", (_PROVIDER,))
    conn.commit()
    conn.close()
    yield


def _embedding_client():
    return EmbeddingClient(config=EmbeddingConfig(provider=_PROVIDER), provider=FakeEmbeddingProvider())


def _run_pipeline_against_pgvector(tmp_path):
    config = CorpusConfig(cache_dir=str(tmp_path))  # full seed-fixture corpus (5 encounters, >1 patient)
    store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    result = run_pipeline(config=config, embedding_client=_embedding_client(), vector_store=store)
    return result, store


def test_migration_010_extension_and_table_are_present():
    store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    with store._conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert cur.fetchone() is not None, "CREATE EXTENSION vector from migration 010 is missing"
        cur.execute("SELECT to_regclass('rag_embeddings') IS NOT NULL")
        assert cur.fetchone()[0] is True, "rag_embeddings table from migration 010 is missing"


def test_second_pipeline_run_over_the_same_corpus_writes_nothing_new(tmp_path):
    first_result, _ = _run_pipeline_against_pgvector(tmp_path)
    assert first_result.index_result.written == len(first_result.corpus)
    assert first_result.index_result.skipped == 0

    second_result, _ = _run_pipeline_against_pgvector(tmp_path)
    assert second_result.index_result.written == 0
    assert second_result.index_result.skipped == len(second_result.corpus)


def test_pgvector_retrieval_matches_in_memory_cosine_ranking(tmp_path):
    pipeline_result, pg_store = _run_pipeline_against_pgvector(tmp_path)

    memory_store = InMemoryCosineStore()
    memory_store.index(pipeline_result.corpus, pipeline_result.vectors_by_record_id)

    query_record = pipeline_result.corpus[0]
    query_vector = pipeline_result.vectors_by_record_id[query_record.record_id]

    pgvector_top = [r.record_id for r in pg_store.retrieve_top_k(query_vector, k=3)]
    memory_top = [r.record_id for r in memory_store.retrieve_top_k(query_vector, k=3)]

    assert pgvector_top == memory_top


def test_pgvector_cross_patient_query_returns_nothing(tmp_path):
    pipeline_result, pg_store = _run_pipeline_against_pgvector(tmp_path)
    corpus = pipeline_result.corpus
    patient_ids = {record.patient_id for record in corpus}
    assert len(patient_ids) > 1, "fixture corpus must span more than one patient for this test to mean anything"

    target = corpus[0]
    other_patient_id = next(pid for pid in patient_ids if pid != target.patient_id)
    # target's own vector is by construction its own closest match — proves
    # a genuinely relevant record is still excluded once scoped elsewhere.
    query_vector = pipeline_result.vectors_by_record_id[target.record_id]

    scoped_result = pg_store.retrieve_top_k(query_vector, k=len(corpus), patient_id=other_patient_id)

    assert target.record_id not in {record.record_id for record in scoped_result}
    assert all(record.patient_id == other_patient_id for record in scoped_result)


def test_pgvector_query_scoped_to_a_patient_with_no_rows_returns_empty(tmp_path):
    pipeline_result, pg_store = _run_pipeline_against_pgvector(tmp_path)
    query_vector = pipeline_result.vectors_by_record_id[pipeline_result.corpus[0].record_id]

    result = pg_store.retrieve_top_k(query_vector, k=5, patient_id=-1)

    assert result == []
