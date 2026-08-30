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

Runs entirely inside a uniquely named temporary Postgres schema (created and
dropped by the module-scoped `_isolated_schema` fixture below) so it never
touches the real policy_documents/policy_chunks/policy_chunk_embeddings rows
in `public` — this module previously deleted those tables unconditionally and
destroyed a real Bedrock-ingested corpus that happened to share the same
live database. `_require_isolated_schema` fails closed rather than deleting
anything if a connection's active schema is ever not the isolated one.

Run with:  pytest -m integration tests/integration/test_policy_corpus_pipeline.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import hashlib
import json
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("pgvector")
from pgvector import Vector  # noqa: E402
from pgvector.psycopg2 import register_vector  # noqa: E402

from libs.policy_corpus.persistence import ingest_corpus  # noqa: E402
from libs.policy_corpus.retrieval import PolicyRetriever, RetrievalScope  # noqa: E402
from libs.policy_corpus.manifest import load_ingestable_documents  # noqa: E402

pytestmark = pytest.mark.integration

_PROVIDER = "fake-titan"
_MODEL = "deterministic-test-v1"
_DIMENSION = 1024
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MANIFEST_PATH = os.path.join(_REPO_ROOT, "docs", "RagDocs", "manifest.json")
_MIGRATION_PATHS = (
    os.path.join(_REPO_ROOT, "db", "migrations", "024_policy_corpus.sql"),
    os.path.join(_REPO_ROOT, "db", "migrations", "025_policy_chunk_embeddings.sql"),
)
_TEST_SCHEMA = f"policy_corpus_test_{uuid.uuid4().hex[:12]}"


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


def _bare_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def _admin_connection():
    """CREATE/DROP SCHEMA needs the admin role — since P3 role separation
    (migration 028), the runtime role (riverbend_app, used by
    _bare_connection above for everything else) has no database-level
    CREATE privilege at all. Schema setup/teardown is the one place this
    file needs elevated access; every other connection here still uses the
    ordinary runtime role, matching production. Discovered and fixed the
    same way in tests/integration/test_policy_corpus_prepare_idempotency.py
    (W10 Final 2 Stage 2) — this file predates that fix."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_ADMIN_USER", "riverbend_admin"),
        password=os.getenv("DB_ADMIN_PASSWORD", "changeme"),
    )


def _connection():
    """Every connection this module hands out is pinned to the isolated
    test schema first, `public` second — so an unqualified `policy_documents`
    etc. always resolves to the throwaway copy, never the real one."""
    conn = _bare_connection()
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
    conn.commit()
    register_vector(conn)
    return conn


def _require_isolated_schema(cur):
    """Fail closed instead of deleting anything if the active schema for
    unqualified names isn't the isolated one — in particular, never `public`,
    which is where the real corpus lives."""
    cur.execute("SELECT current_schema()")
    active = cur.fetchone()[0]
    assert active == _TEST_SCHEMA, (
        f"refusing destructive cleanup: active schema is {active!r}, "
        f"not the isolated test schema {_TEST_SCHEMA!r}"
    )


@pytest.fixture(scope="module", autouse=True)
def _isolated_schema():
    app_role = os.getenv("DB_USER", "riverbend_app")
    setup_conn = _admin_connection()
    setup_conn.autocommit = True
    with setup_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {_TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
        for path in _MIGRATION_PATHS:
            with open(path, encoding="utf-8") as f:
                cur.execute(f.read())
        # The runtime role's table/sequence privileges (migration 028) are
        # scoped to `IN SCHEMA public` only — a schema this test creates on
        # the fly needs its own explicit grant, or every _connection() below
        # (the ordinary app role, matching production) could create the
        # schema's TABLES but never read/write rows in them.
        cur.execute(f"GRANT USAGE ON SCHEMA {_TEST_SCHEMA} TO {app_role}")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {_TEST_SCHEMA} TO {app_role}")
        cur.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {_TEST_SCHEMA} TO {app_role}")
    setup_conn.close()

    yield

    teardown_conn = _admin_connection()
    teardown_conn.autocommit = True
    with teardown_conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
    teardown_conn.close()


@pytest.fixture(autouse=True)
def _clean_policy_tables():
    conn = _connection()
    with conn.cursor() as cur:
        _require_isolated_schema(cur)
        cur.execute("DELETE FROM policy_chunk_embeddings")
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
    assert first.documents_upserted == len(load_ingestable_documents(_MANIFEST_PATH))
    assert first.chunks_written > 0
    assert first.embeddings_written == first.chunks_written

    second = _ingest()
    assert second.chunks_written == 0
    assert second.embeddings_written == 0
    assert second.chunks_skipped == first.chunks_written
    assert second.embeddings_skipped == first.chunks_written


def test_a_document_dropped_from_the_manifest_becomes_unretrievable_after_reingestion(tmp_path):
    # Review fix STALE-RETRIEVAL: a document's stored retrieval_enabled must
    # actually flip false on reingestion once it's no longer in the
    # manifest's ingestable set — not just rely on a filter that only ever
    # reflects whatever the row said the last time it WAS ingestable.
    _ingest()
    real_content_root = os.path.dirname(_MANIFEST_PATH)
    manifest = json.loads(open(_MANIFEST_PATH, encoding="utf-8").read())
    manifest["ingestion"]["content_root"] = real_content_root
    dropped_index = next(
        index for index, document in enumerate(manifest["documents"])
        if document["source_id"] == "GUIDE-COVERAGE-ELIG-001"
    )
    dropped = manifest["documents"].pop(dropped_index)
    kept_source_id = manifest["documents"][0]["source_id"]
    reduced_manifest_path = tmp_path / "manifest.json"
    reduced_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    conn = _connection()
    report = ingest_corpus(
        conn, str(reduced_manifest_path), _DeterministicEmbeddingClient(),
        provider=_PROVIDER, model=_MODEL, expected_dimension=_DIMENSION, vector_cast=Vector,
    )
    conn.close()
    assert report.documents_deactivated == 1

    scope = RetrievalScope(audiences=tuple(dropped["audiences"]), workflows=tuple(dropped["workflows"]))
    results = _retriever().retrieve("what does active coverage status mean?", scope, limit=10)
    assert all(r.source_id != dropped["source_id"] for r in results)

    # the untouched document must remain retrievable — deactivation is
    # scoped to exactly what left the manifest, not a blanket wipe.
    kept_scope = RetrievalScope(audiences=("patient", "clinician", "nursing_ma"), workflows=("patient_summary",))
    kept_results = _retriever().retrieve("laboratory result release", kept_scope, limit=10)
    assert any(r.source_id == kept_source_id for r in kept_results)


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
