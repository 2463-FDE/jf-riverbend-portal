#!/usr/bin/env python3
"""
Idempotent entry point for the policy corpus's real ingestion path
(w-9-2-planner P3): validates docs/RagDocs/manifest.json, chunks every
ingestable document, embeds each chunk with the real Bedrock provider, and
persists documents/chunks/embeddings — deactivating (never deleting) any
document no longer in the manifest's current ingestable set.

Safe to run repeatedly against the SAME corpus: unchanged chunks are never
re-embedded (skip-by-content-hash, before the provider is ever called), and
this script itself does nothing but wire real infrastructure into
libs/policy_corpus/persistence.py::ingest_corpus, which owns the actual
idempotency/dimension/deactivation behavior (already tested at the unit and
integration level — see tests/test_policy_persistence.py and
tests/integration/test_policy_corpus_pipeline.py).

Requires POLICY_EMBEDDING_MODEL_ID and AWS_REGION configured (see
.env.example) and a reachable Postgres with migrations 024/025 applied.

Run:  python3 db/policy_corpus_ingest.py   (against a running `make up` stack)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2  # noqa: E402
from pgvector import Vector  # noqa: E402
from pgvector.psycopg2 import register_vector  # noqa: E402

from libs.embedding_client import EmbeddingClient, EmbeddingConfig  # noqa: E402
from libs.policy_corpus import BedrockPolicyEmbeddingProvider, ingest_corpus  # noqa: E402

_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "RagDocs", "manifest.json")
_PROVIDER = "bedrock"
_DIMENSION = 1024  # amazon.titan-embed-text-v2:0 — see migration 025's own note on changing this


def main() -> None:
    model_id = os.getenv("POLICY_EMBEDDING_MODEL_ID", "")
    if not model_id or model_id == "changeme":
        raise SystemExit("POLICY_EMBEDDING_MODEL_ID is not configured — see .env.example")

    # Review fix PN-CONN-LEAK: validate the embedding provider (which also
    # requires AWS_REGION) BEFORE opening any Postgres connection — the old
    # order left a connection open with nothing to close it whenever
    # BedrockPolicyEmbeddingProvider's own construction failed.
    embedding_client = EmbeddingClient(
        config=EmbeddingConfig(provider=_PROVIDER),
        provider=BedrockPolicyEmbeddingProvider(model_id=model_id),
    )

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        # register_vector and ingest_corpus are both inside this try: any
        # failure in either — not just ingest_corpus — must still close the
        # connection rather than leaking it.
        register_vector(conn)
        report = ingest_corpus(
            conn, _MANIFEST_PATH, embedding_client,
            provider=_PROVIDER, model=model_id, expected_dimension=_DIMENSION, vector_cast=Vector,
        )
    finally:
        conn.close()

    print(
        f"documents_upserted={report.documents_upserted} "
        f"documents_deactivated={report.documents_deactivated} "
        f"chunks_written={report.chunks_written} chunks_skipped={report.chunks_skipped} "
        f"embeddings_written={report.embeddings_written} embeddings_skipped={report.embeddings_skipped}"
    )


if __name__ == "__main__":
    main()
