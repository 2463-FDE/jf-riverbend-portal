"""Integration test — requires the full stack up (`make up`) on a fresh
volume (migrations 024/025 present). The "focused retrieval evaluation"
w-9-2-planner P2 asks for: idempotent ingestion, metadata-filtered
retrieval, and a deterministic-keyword-baseline comparison, all against a
REAL Postgres/pgvector — proving what tests/test_policy_persistence.py and
tests/test_policy_retrieval.py's fake connections cannot.

Uses a deterministic local vector generator, not the real Bedrock adapter
(already proven at the unit level in tests/test_policy_embedding_provider.py
against mocked boto3) — this test is about the SQL/schema, not the network
call, and must stay runnable with no live AWS credential.

Run with:  pytest -m integration tests/integration/test_policy_corpus_pipeline.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import hashlib
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("pgvector")
from pgvector import Vector  # noqa: E402
from pgvector.psycopg2 import register_vector  # noqa: E402

from libs.policy_corpus.persistence import ingest_corpus  # noqa: E402
from libs.policy_corpus.retrieval import PolicyRetriever, RetrievalScope  # noqa: E402

pytestmark = pytest.mark.integration

_PROVIDER = "fake-titan"
_MODEL = "deterministic-test-v1"
_DIMENSION = 1024
_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs", "RagDocs", "manifest.json")


def _fake_vector(text: str):
    # Feature-hashing ("hashing trick") bag-of-words, not a raw text hash:
    # texts sharing words land nonzero mass on the same dimensions, so
    # cosine similarity genuinely tracks word overlap. Deterministic and
    # network-free, but — unlike a plain digest-of-the-whole-string hash —
    # actually meaningful enough to test ranking against a keyword baseline.
    vec = [0.0] * _DIMENSION
    for word in text.lower().split():
        idx = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16) % _DIMENSION
        vec[idx] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec] if norm else vec


class _DeterministicEmbeddingClient:
    """Stands in for embedding_client.EmbeddingClient — same .embed(texts)
    shape, no network, but produces real 1024-dim vectors so they fit the
    schema's actual VECTOR(1024) column (the real Bedrock adapter's
    request/response mechanics are already covered at the unit level)."""

    def embed(self, texts):
        return [_fake_vector(t) for t in texts]


def _connection():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )
    register_vector(conn)
    return conn


@pytest.fixture(autouse=True)
def _clean_policy_tables():
    conn = _connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM policy_chunk_embeddings WHERE provider = %s", (_PROVIDER,))
        cur.execute("DELETE FROM policy_chunks")
        cur.execute("DELETE FROM policy_documents")
    conn.commit()
    conn.close()
    yield


def _ingest():
    conn = _connection()
    report = ingest_corpus(
        conn, _MANIFEST_PATH, _DeterministicEmbeddingClient(),
        provider=_PROVIDER, model=_MODEL, expected_dimension=_DIMENSION, vector_cast=Vector,
    )
    conn.close()
    return report


def test_migration_024_and_025_tables_are_present():
    conn = _connection()
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('policy_documents') IS NOT NULL")
        assert cur.fetchone()[0] is True
        cur.execute("SELECT to_regclass('policy_chunk_embeddings') IS NOT NULL")
        assert cur.fetchone()[0] is True
    conn.close()


def test_first_ingest_writes_every_chunk_and_embedding_then_second_is_a_no_op():
    first = _ingest()
    assert first.documents_upserted == 11
    assert first.chunks_written > 0
    assert first.embeddings_written == first.chunks_written

    second = _ingest()
    assert second.chunks_written == 0
    assert second.embeddings_written == 0
    assert second.chunks_skipped == first.chunks_written
    assert second.embeddings_skipped == first.chunks_written


def _retriever():
    return PolicyRetriever(
        _connection(), _DeterministicEmbeddingClient(), provider=_PROVIDER, model=_MODEL, vector_cast=Vector
    )


def test_a_scoped_query_retrieves_the_document_authorized_for_that_workflow():
    _ingest()
    scope = RetrievalScope(audiences=("front_desk",), workflows=("coverage_eligibility",))

    results = _retriever().retrieve("what does active coverage status mean?", scope, limit=5)

    assert results
    assert all(r.source_id == "GUIDE-COVERAGE-ELIG-001" for r in results)


def test_unauthorized_workflow_scope_retrieves_nothing_from_that_document():
    # The core security property: GUIDE-COVERAGE-ELIG-001 is scoped ONLY to
    # front_desk/billing + coverage_eligibility — a patient/patient_summary
    # scope must retrieve zero chunks from it, regardless of similarity.
    _ingest()
    scope = RetrievalScope(audiences=("patient",), workflows=("patient_summary",))

    results = _retriever().retrieve("what does active coverage status mean?", scope, limit=10)

    assert all(r.source_id != "GUIDE-COVERAGE-ELIG-001" for r in results)


def test_vector_retrieval_agrees_with_a_deterministic_keyword_baseline():
    # A minimal instance of vector-rag.md's "compare against a deterministic
    # metadata/keyword baseline" — proves the vector path isn't returning
    # something the simplest possible check would call irrelevant.
    _ingest()
    scope = RetrievalScope(audiences=("roi_clerk",), workflows=("roi",))

    [top] = _retriever().retrieve("minimum necessary disclosure accounting", scope, limit=1)

    baseline_keywords = {"disclosure", "accounting", "minimum", "necessary"}
    assert baseline_keywords & set(top.text.lower().split())
