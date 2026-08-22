"""Regression test for the pgvector dimension drift between the flattened
`db/schema.sql` (what a fresh `make up` volume actually gets) and
`db/migrations/010_pgvector_embeddings.sql` (the incremental migration
`db/migrations/apply.sh` re-runs against every deployed database).

The two had silently diverged: schema.sql declared `VECTOR(768)` (a leftover
from a discarded Ollama nomic-embed-text plan — the misleading comment even
said "see migration 011", which is the unrelated demographics-splitting
migration) while migration 010 has always declared `VECTOR(16)`, matching
`FakeEmbeddingProvider`'s fixed 16-dimensional output
(libs/embedding_client/providers/fake_provider.py::_DIMENSIONS). A fresh
volume and an incrementally-migrated one would build `rag_embeddings.embedding`
at two different, incompatible dimensions, and neither matched what the
embedding pipeline actually produces.

This is a pure text/import check — no live Postgres required — so it runs in
`make test`, not just the integration suite, and fails fast the next time
either file's VECTOR(N) or the fake provider's dimension changes without the
other two being updated to match.
"""
import re
from pathlib import Path

from libs.embedding_client.providers.fake_provider import _DIMENSIONS as FAKE_PROVIDER_DIMENSIONS

REPO_ROOT = Path(__file__).resolve().parents[1]


def _rag_embeddings_vector_dimension(sql_text: str) -> int:
    """Extract the N in `embedding    VECTOR(N) NOT NULL` for rag_embeddings."""
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS rag_embeddings\s*\((.*?)\);",
        sql_text,
        re.DOTALL,
    )
    assert match, "rag_embeddings table definition not found"
    body = match.group(1)
    vector_match = re.search(r"embedding\s+VECTOR\((\d+)\)", body)
    assert vector_match, "rag_embeddings.embedding VECTOR(N) column not found"
    return int(vector_match.group(1))


def test_schema_sql_and_migration_010_agree_on_the_embedding_dimension():
    schema_sql = (REPO_ROOT / "db" / "schema.sql").read_text()
    migration_010 = (REPO_ROOT / "db" / "migrations" / "010_pgvector_embeddings.sql").read_text()

    schema_dim = _rag_embeddings_vector_dimension(schema_sql)
    migration_dim = _rag_embeddings_vector_dimension(migration_010)

    assert schema_dim == migration_dim, (
        f"db/schema.sql declares VECTOR({schema_dim}) for rag_embeddings.embedding "
        f"but db/migrations/010_pgvector_embeddings.sql declares VECTOR({migration_dim}). "
        "A fresh-volume database and an incrementally-migrated one would build this "
        "column at different, incompatible dimensions — keep both files in sync."
    )


def test_the_shared_dimension_matches_the_fake_embedding_provider():
    schema_sql = (REPO_ROOT / "db" / "schema.sql").read_text()
    schema_dim = _rag_embeddings_vector_dimension(schema_sql)

    assert schema_dim == FAKE_PROVIDER_DIMENSIONS, (
        f"rag_embeddings.embedding is VECTOR({schema_dim}) but "
        f"FakeEmbeddingProvider produces {FAKE_PROVIDER_DIMENSIONS}-dimensional "
        "vectors — the safe default provider's own output would not fit the column "
        "it is meant to write to."
    )
