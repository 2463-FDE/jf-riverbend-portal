#!/usr/bin/env python3
"""
W10 Final 2 Stage 2 — one idempotent operator command that makes the
approved synthetic policy corpus ready, on any clean environment, without
an undocumented host Python environment.

Run via `make rag-prepare`, which execs this INSIDE records-service's own
container — it already carries the pinned boto3/pgvector/psycopg2
dependencies db/policy_corpus_ingest.py and db/policy_corpus_evaluate.py
also need (see services/records-service/requirements.txt); this script
never assumes a host Python install has them.

Checks freshness first (a DB-only comparison — see
libs/policy_corpus/freshness.py::check_corpus_freshness, no AWS_REGION or
embedding-provider construction needed for this step) and only ingests when
the manifest and database actually disagree. An already-fresh corpus never
constructs BedrockPolicyEmbeddingProvider/EmbeddingClient at all, so it
makes zero embedding calls, not merely zero *unnecessary* ones. Ingestion
itself (db/policy_corpus_ingest.py's own libs.policy_corpus.ingest_corpus)
is separately idempotent per chunk (skip-by-content-hash before the
provider is ever called) and only ever deactivates documents that left the
CURRENT manifest — it never deletes anything, and never touches another
environment's corpus (each environment's own Postgres is a separate
database, per adr/0001 and this repo's per-service DB access model).

No corpus/freshness logic is duplicated here — this file only sequences
calls into libs.policy_corpus, the same contracts
db/policy_corpus_ingest.py and db/policy_corpus_evaluate.py already use.

Required configuration is validated by presence/non-placeholder status
only, before any Postgres connection or provider construction — never
printed or logged. Real Bedrock calls only happen when genuinely needed
(missing/stale content); CI never runs this script (see Makefile — no CI
job references `rag-prepare`), only a local operator with real AWS
credentials in .env does.

Run:  make rag-prepare   (needs `make up` already running)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2  # noqa: E402
from pgvector import Vector  # noqa: E402
from pgvector.psycopg2 import register_vector  # noqa: E402

from libs.embedding_client import EmbeddingClient, EmbeddingConfig  # noqa: E402
from libs.policy_corpus import (  # noqa: E402
    BedrockPolicyEmbeddingProvider,
    check_corpus_freshness,
    ingest_corpus,
)

_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "RagDocs", "manifest.json")
_PROVIDER = "bedrock"
_DIMENSION = 1024  # amazon.titan-embed-text-v2:0 — see migration 025's own note on changing this

# Presence/non-placeholder only — values are never read into a log line or
# printed anywhere in this module.
_REQUIRED_VARS = ("POLICY_EMBEDDING_MODEL_ID", "AWS_REGION", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
_PLACEHOLDER_VALUES = {"", "changeme"}


def _validate_configuration() -> str:
    """Fails before any Postgres connection or provider construction — same
    ordering discipline as policy_corpus_ingest.py's own main() (review fix
    PN-CONN-LEAK). Returns the validated POLICY_EMBEDDING_MODEL_ID."""
    missing = sorted(name for name in _REQUIRED_VARS if os.getenv(name, "") in _PLACEHOLDER_VALUES)
    if missing:
        raise SystemExit(
            f"missing or placeholder configuration: {', '.join(missing)} — see .env.example"
        )
    return os.environ["POLICY_EMBEDDING_MODEL_ID"]


def _connect():
    return psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.getenv("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
    )


def main() -> int:
    model_id = _validate_configuration()

    conn = _connect()
    try:
        register_vector(conn)
        freshness = check_corpus_freshness(conn, _MANIFEST_PATH, provider=_PROVIDER, model=model_id)
        if freshness.is_fresh:
            print(
                f"status=fresh action=skipped corpus_id={freshness.corpus_id} "
                f"documents={freshness.database_documents} chunks={freshness.embedded_chunks}"
            )
            return 0

        embedding_client = EmbeddingClient(
            config=EmbeddingConfig(provider=_PROVIDER),
            provider=BedrockPolicyEmbeddingProvider(model_id=model_id),
        )
        report = ingest_corpus(
            conn, _MANIFEST_PATH, embedding_client,
            provider=_PROVIDER, model=model_id, expected_dimension=_DIMENSION, vector_cast=Vector,
        )
        freshness_after = check_corpus_freshness(conn, _MANIFEST_PATH, provider=_PROVIDER, model=model_id)
        status = "ready" if freshness_after.is_fresh else "still_stale"
        print(
            f"status={status} action=ingested corpus_id={freshness_after.corpus_id} "
            f"documents_upserted={report.documents_upserted} documents_deactivated={report.documents_deactivated} "
            f"chunks_written={report.chunks_written} chunks_skipped={report.chunks_skipped} "
            f"embeddings_written={report.embeddings_written} embeddings_skipped={report.embeddings_skipped}"
        )
        return 0 if freshness_after.is_fresh else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
