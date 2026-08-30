"""Integration test — requires the full stack up (`make up`) on a fresh
volume (migrations 024/025 present). Proves db/policy_corpus_prepare.py's
own end-to-end idempotency against a REAL Postgres/pgvector, through the
wrapper's actual main() — not just libs.policy_corpus.ingest_corpus, which
already has its own integration coverage in
tests/integration/test_policy_corpus_pipeline.py.

Same isolated-schema-per-module discipline as that sibling file (never
touches the real policy_documents/policy_chunks/policy_chunk_embeddings
rows in `public` — the reusable local demo corpus), and the same
deterministic local vector generator instead of the real Bedrock adapter,
so this stays runnable with no live AWS credential.

Run with:  pytest -m integration tests/integration/test_policy_corpus_prepare_script.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import hashlib
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("pgvector")
from pgvector.psycopg2 import register_vector  # noqa: E402

from conftest import load_module  # noqa: E402

prepare_mod = load_module("db/policy_corpus_prepare.py", "policy_corpus_prepare_script_integration")

pytestmark = pytest.mark.integration

_DIMENSION = 1024
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MIGRATION_PATHS = (
    os.path.join(_REPO_ROOT, "db", "migrations", "024_policy_corpus.sql"),
    os.path.join(_REPO_ROOT, "db", "migrations", "025_policy_chunk_embeddings.sql"),
)
_TEST_SCHEMA = f"policy_corpus_prepare_test_{uuid.uuid4().hex[:12]}"


def _fake_vector(text: str):
    vec = [0.0] * _DIMENSION
    for word in text.lower().split():
        idx = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16) % _DIMENSION
        vec[idx] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec] if norm else vec


class _DeterministicEmbeddingClient:
    def embed(self, texts):
        return [_fake_vector(t) for t in texts]


def _bare_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def _admin_connection():
    """CREATE SCHEMA needs the admin role — since P3 role separation
    (migration 028), the runtime role (riverbend_app, used by
    _bare_connection above and by the script under test) has no database-
    level CREATE privilege at all (`has_database_privilege('riverbend_app',
    'riverbend', 'CREATE')` is false), same as db/schema.sql's own reasoning
    for running as admin. Schema setup/teardown is the one place this file
    needs elevated access; the script under test still runs only as the
    ordinary runtime role, matching production."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_ADMIN_USER", "riverbend_admin"),
        password=os.getenv("DB_ADMIN_PASSWORD", "changeme"),
    )


def _schema_scoped_connection():
    """Pinned to the isolated test schema first, `public` second — an
    unqualified policy_documents etc. always resolves to the throwaway
    copy, never the real one."""
    conn = _bare_connection()
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
    conn.commit()
    register_vector(conn)
    return conn


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
        # the fly needs its own explicit grant, or ingest_corpus's own
        # connection (the ordinary app role, matching production) could
        # create the schema's TABLES but never read/write rows in them.
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
def _valid_env(monkeypatch):
    monkeypatch.setenv("POLICY_EMBEDDING_MODEL_ID", "deterministic-test-v1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("DB_HOST", os.getenv("DB_HOST", "localhost"))
    monkeypatch.setenv("DB_NAME", os.getenv("DB_NAME", "riverbend"))
    monkeypatch.setenv("DB_USER", os.getenv("DB_USER", "riverbend_app"))
    monkeypatch.setenv("DB_PASSWORD", os.getenv("DB_PASSWORD", "changeme"))
    # main() opens its own connection via _connect() — swap that one
    # function for one pinned to the isolated schema; everything else in
    # main() (register_vector, ingest_corpus, check_corpus_freshness) runs
    # completely unmodified against it. Patching prepare_mod._connect
    # itself, not prepare_mod.psycopg2.connect: the latter IS this test
    # file's own `psycopg2` module object too (the same sys.modules entry),
    # so patching its `connect` attribute would recurse into itself the
    # moment _bare_connection/_admin_connection also called psycopg2.connect.
    monkeypatch.setattr(prepare_mod, "_connect", _schema_scoped_connection)
    # No real Bedrock call, ever, in this file — same reasoning as
    # test_policy_corpus_pipeline.py.
    monkeypatch.setattr(prepare_mod, "BedrockPolicyEmbeddingProvider", lambda **kwargs: object())
    monkeypatch.setattr(prepare_mod, "EmbeddingClient", lambda **kwargs: _DeterministicEmbeddingClient())


@pytest.fixture(autouse=True)
def _clean_policy_tables():
    conn = _schema_scoped_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT current_schema()")
        assert cur.fetchone()[0] == _TEST_SCHEMA, "refusing to clean a non-isolated schema"
        cur.execute("DELETE FROM policy_chunk_embeddings")
        cur.execute("DELETE FROM policy_chunks")
        cur.execute("DELETE FROM policy_documents")
    conn.commit()
    conn.close()
    yield


def test_a_clean_isolated_database_becomes_ready():
    result = prepare_mod.main()
    assert result == 0


def test_a_second_run_is_idempotent_and_makes_zero_embedding_calls(monkeypatch):
    first_result = prepare_mod.main()
    assert first_result == 0

    embed_calls = []
    monkeypatch.setattr(_DeterministicEmbeddingClient, "embed", lambda self, texts: embed_calls.append(len(texts)))

    second_result = prepare_mod.main()

    assert second_result == 0
    assert embed_calls == [], "an already-fresh corpus must make zero embedding calls, not merely zero unnecessary ones"
