"""Idempotent persistence for the policy corpus (w-9-2-planner P2,
embeddings/retrieval slice): upserts document/chunk metadata and
dimension-enforced embeddings against policy_documents/policy_chunks/
policy_chunk_embeddings (migrations 024/025). `conn` is
dependency-injectable (mirrors libs/rag_corpus/vector_store.py::
PgVectorStore) — tests drive this against a fake psycopg2-shaped double.

Idempotent by content hash, never by presence, and nothing here ever
DELETEs a row: a chunk/document that stops appearing in a later run becomes
unreachable through retrieval's own filters (retrieval_enabled, current
provider/model) instead of being destroyed — preserving audit-relevant
history (vector-rag.md's ingestion contract). The embedding provider call
itself is skipped for any chunk whose content_hash is already stored under
the current (provider, model); only a changed or new chunk is re-embedded.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .contracts import PolicyChunk, PolicyDocumentMeta
from .manifest import load_ingestable_documents, load_manifest
from .chunking import chunk_markdown


class DimensionMismatchError(ValueError):
    """A vector's length didn't match the expected embedding dimension for
    its (provider, model) — refused before it ever reaches Postgres, per
    vector-rag.md: "Refuse indexing or querying when dimensions differ."
    (Postgres's own VECTOR(1024) column would also reject a wrong-length
    vector, but this gives a clear, application-level error first.)"""


@dataclass(frozen=True)
class IngestionReport:
    documents_upserted: int
    chunks_written: int
    chunks_skipped: int
    embeddings_written: int
    embeddings_skipped: int


def upsert_document(conn, doc: PolicyDocumentMeta, *, corpus_id: str) -> int:
    """Insert or refresh one policy_documents row, keyed on
    (source_id, source_version). Returns the row id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO policy_documents
                (corpus_id, source_id, source_version, title, owner, effective_date,
                 approval_status, synthetic, retrieval_enabled, content_path, content_sha256,
                 audiences, workflows, topics, allowed_uses, prohibited_uses, relationships, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (source_id, source_version) DO UPDATE
                SET corpus_id = EXCLUDED.corpus_id, title = EXCLUDED.title, owner = EXCLUDED.owner,
                    effective_date = EXCLUDED.effective_date, approval_status = EXCLUDED.approval_status,
                    synthetic = EXCLUDED.synthetic, retrieval_enabled = EXCLUDED.retrieval_enabled,
                    content_path = EXCLUDED.content_path, content_sha256 = EXCLUDED.content_sha256,
                    audiences = EXCLUDED.audiences, workflows = EXCLUDED.workflows, topics = EXCLUDED.topics,
                    allowed_uses = EXCLUDED.allowed_uses, prohibited_uses = EXCLUDED.prohibited_uses,
                    relationships = EXCLUDED.relationships, updated_at = now()
            RETURNING id
            """,
            (
                corpus_id, doc.source_id, doc.source_version, doc.title, doc.owner, doc.effective_date,
                doc.approval_status, doc.synthetic, doc.retrieval_enabled, doc.content_path, doc.content_sha256,
                list(doc.audiences), list(doc.workflows), list(doc.topics),
                list(doc.allowed_uses), list(doc.prohibited_uses), json.dumps(list(doc.relationships)),
            ),
        )
        return cur.fetchone()[0]


def upsert_chunks(conn, document_id: int, chunks: List[PolicyChunk]) -> Tuple[int, int]:
    """Returns (written, skipped). A chunk whose chunk_hash is unchanged
    from what's stored is skipped — its row is left exactly as it was."""
    written = skipped = 0
    with conn.cursor() as cur:
        for chunk in chunks:
            cur.execute(
                """
                INSERT INTO policy_chunks
                    (chunk_id, document_id, section_id, heading_path, text, chunk_hash, char_count, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (chunk_id) DO UPDATE
                    SET document_id = EXCLUDED.document_id, section_id = EXCLUDED.section_id,
                        heading_path = EXCLUDED.heading_path, text = EXCLUDED.text,
                        chunk_hash = EXCLUDED.chunk_hash, char_count = EXCLUDED.char_count, updated_at = now()
                    WHERE policy_chunks.chunk_hash IS DISTINCT FROM EXCLUDED.chunk_hash
                """,
                (chunk.chunk_id, document_id, chunk.section_id, list(chunk.heading_path),
                 chunk.text, chunk.chunk_hash, chunk.char_count),
            )
            if cur.rowcount:
                written += 1
            else:
                skipped += 1
    return written, skipped


def existing_embedding_hashes(conn, chunk_ids: List[str], *, provider: str, model: str) -> Dict[str, str]:
    """chunk_id -> content_hash already stored for this (provider, model) —
    the lookup that lets ingest_corpus skip re-embedding an unchanged chunk
    entirely, before ever calling the (expensive, real) embedding provider."""
    if not chunk_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_id, content_hash FROM policy_chunk_embeddings "
            "WHERE provider = %s AND model = %s AND chunk_id = ANY(%s)",
            (provider, model, chunk_ids),
        )
        return dict(cur.fetchall())


def upsert_embedding(
    conn, *, chunk_id: str, provider: str, model: str, vector: List[float], chunk_text: str,
    expected_dimension: int = None, vector_cast=None,
) -> None:
    dimension = len(vector)
    if expected_dimension is not None and dimension != expected_dimension:
        raise DimensionMismatchError(
            f"{chunk_id}: expected dimension {expected_dimension}, got {dimension} "
            f"for provider={provider!r} model={model!r}"
        )
    content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
    vector_param = vector_cast(vector) if vector_cast else vector
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO policy_chunk_embeddings (chunk_id, provider, model, dimension, content_hash, embedding, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (chunk_id, provider, model) DO UPDATE
                SET dimension = EXCLUDED.dimension, content_hash = EXCLUDED.content_hash,
                    embedding = EXCLUDED.embedding, updated_at = now()
            """,
            (chunk_id, provider, model, dimension, content_hash, vector_param),
        )


def ingest_corpus(
    conn, manifest_path: str, embedding_client, *, provider: str, model: str,
    expected_dimension: int = None, vector_cast=None,
) -> IngestionReport:
    """The full pipeline: validate+load the manifest, chunk every ingestable
    document, and persist documents/chunks/embeddings — skipping the
    embedding provider call entirely for any chunk whose hash is unchanged.
    Commits once at the end; rolls back and re-raises on any failure, so a
    partial run never leaves a half-written corpus."""
    manifest = load_manifest(manifest_path)
    documents_upserted = 0
    chunks_written = chunks_skipped = 0
    embeddings_written = embeddings_skipped = 0
    try:
        for doc, text in load_ingestable_documents(manifest_path):
            document_id = upsert_document(conn, doc, corpus_id=manifest.corpus_id)
            documents_upserted += 1

            chunks = chunk_markdown(
                source_id=doc.source_id, source_version=doc.source_version,
                markdown_text=text, config=manifest.ingestion.chunking,
            )
            written, skipped = upsert_chunks(conn, document_id, chunks)
            chunks_written += written
            chunks_skipped += skipped

            existing = existing_embedding_hashes(conn, [c.chunk_id for c in chunks], provider=provider, model=model)
            to_embed = [c for c in chunks if existing.get(c.chunk_id) != c.chunk_hash]
            embeddings_skipped += len(chunks) - len(to_embed)
            if to_embed:
                vectors = embedding_client.embed([c.text for c in to_embed])
                for chunk, vector in zip(to_embed, vectors):
                    upsert_embedding(
                        conn, chunk_id=chunk.chunk_id, provider=provider, model=model, vector=vector,
                        chunk_text=chunk.text, expected_dimension=expected_dimension, vector_cast=vector_cast,
                    )
                    embeddings_written += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return IngestionReport(
        documents_upserted=documents_upserted,
        chunks_written=chunks_written,
        chunks_skipped=chunks_skipped,
        embeddings_written=embeddings_written,
        embeddings_skipped=embeddings_skipped,
    )
