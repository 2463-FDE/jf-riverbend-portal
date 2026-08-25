"""Manifest-to-database freshness verification for the policy corpus.

Inventory counts alone cannot prove that the navigator is using the current
documents. This module compares source/version/hash and every deterministic
chunk identity/hash with the embeddings available for one provider/model.
It never selects document text or vector values.
"""
from dataclasses import dataclass
from typing import Tuple

from .chunking import chunk_markdown
from .manifest import load_ingestable_documents, load_manifest


@dataclass(frozen=True)
class CorpusFreshnessReport:
    corpus_id: str
    provider: str
    model: str
    expected_documents: int
    database_documents: int
    expected_chunks: int
    embedded_chunks: int
    dimensions: Tuple[int, ...]
    missing_documents: Tuple[str, ...]
    extra_active_documents: Tuple[str, ...]
    document_hash_mismatches: Tuple[str, ...]
    missing_embeddings: Tuple[str, ...]
    extra_active_embeddings: Tuple[str, ...]
    chunk_hash_mismatches: Tuple[str, ...]
    embedding_hash_mismatches: Tuple[str, ...]

    @property
    def is_fresh(self) -> bool:
        return not any(
            (
                self.missing_documents,
                self.extra_active_documents,
                self.document_hash_mismatches,
                self.missing_embeddings,
                self.extra_active_embeddings,
                self.chunk_hash_mismatches,
                self.embedding_hash_mismatches,
            )
        ) and self.dimensions == (1024,)

    def as_dict(self) -> dict:
        return {
            "corpus_id": self.corpus_id,
            "provider": self.provider,
            "model": self.model,
            "fresh": self.is_fresh,
            "expected_documents": self.expected_documents,
            "database_documents": self.database_documents,
            "expected_chunks": self.expected_chunks,
            "embedded_chunks": self.embedded_chunks,
            "dimensions": list(self.dimensions),
            "missing_documents": list(self.missing_documents),
            "extra_active_documents": list(self.extra_active_documents),
            "document_hash_mismatches": list(self.document_hash_mismatches),
            "missing_embeddings": list(self.missing_embeddings),
            "extra_active_embeddings": list(self.extra_active_embeddings),
            "chunk_hash_mismatches": list(self.chunk_hash_mismatches),
            "embedding_hash_mismatches": list(self.embedding_hash_mismatches),
        }


def _expected(manifest_path: str):
    manifest = load_manifest(manifest_path)
    documents = {}
    chunks = {}
    for doc, text in load_ingestable_documents(manifest_path):
        identity = f"{doc.source_id}@{doc.source_version}"
        documents[identity] = doc.content_sha256
        for chunk in chunk_markdown(
            source_id=doc.source_id,
            source_version=doc.source_version,
            markdown_text=text,
            config=manifest.ingestion.chunking,
        ):
            chunks[chunk.chunk_id] = chunk.chunk_hash
    return manifest.corpus_id, documents, chunks


def check_corpus_freshness(conn, manifest_path: str, *, provider: str, model: str) -> CorpusFreshnessReport:
    """Compare the current ingestable manifest with active database rows.

    Historical disabled documents and embeddings for other provider/model
    spaces may remain for auditability; they are not considered active extras.
    Active documents from another corpus ARE extras because PolicyRetriever's
    runtime query does not filter by corpus_id and could return them.
    """
    corpus_id, expected_documents, expected_chunks = _expected(manifest_path)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_id, source_version, content_sha256
            FROM policy_documents
            WHERE retrieval_enabled AND synthetic
              AND approval_status = 'approved_training'
            """,
            (),
        )
        database_documents = {
            f"{source_id}@{source_version}": content_hash
            for source_id, source_version, content_hash in cur.fetchall()
        }

        cur.execute(
            """
            SELECT c.chunk_id, c.chunk_hash, e.content_hash, e.dimension
            FROM policy_documents d
            JOIN policy_chunks c ON c.document_id = d.id
            JOIN policy_chunk_embeddings e ON e.chunk_id = c.chunk_id
            WHERE d.retrieval_enabled AND d.synthetic
              AND d.approval_status = 'approved_training'
              AND e.provider = %s AND e.model = %s
            """,
            (provider, model),
        )
        embedding_rows = {
            chunk_id: (chunk_hash, embedding_hash, dimension)
            for chunk_id, chunk_hash, embedding_hash, dimension in cur.fetchall()
        }

    conn.rollback()

    expected_doc_ids = set(expected_documents)
    database_doc_ids = set(database_documents)
    expected_chunk_ids = set(expected_chunks)
    database_chunk_ids = set(embedding_rows)

    return CorpusFreshnessReport(
        corpus_id=corpus_id,
        provider=provider,
        model=model,
        expected_documents=len(expected_documents),
        database_documents=len(database_documents),
        expected_chunks=len(expected_chunks),
        embedded_chunks=len(embedding_rows),
        dimensions=tuple(sorted({row[2] for row in embedding_rows.values()})),
        missing_documents=tuple(sorted(expected_doc_ids - database_doc_ids)),
        extra_active_documents=tuple(sorted(database_doc_ids - expected_doc_ids)),
        document_hash_mismatches=tuple(
            sorted(
                identity
                for identity in expected_doc_ids & database_doc_ids
                if expected_documents[identity] != database_documents[identity]
            )
        ),
        missing_embeddings=tuple(sorted(expected_chunk_ids - database_chunk_ids)),
        extra_active_embeddings=tuple(sorted(database_chunk_ids - expected_chunk_ids)),
        chunk_hash_mismatches=tuple(
            sorted(
                chunk_id
                for chunk_id in expected_chunk_ids & database_chunk_ids
                if expected_chunks[chunk_id] != embedding_rows[chunk_id][0]
            )
        ),
        embedding_hash_mismatches=tuple(
            sorted(
                chunk_id
                for chunk_id in expected_chunk_ids & database_chunk_ids
                if expected_chunks[chunk_id] != embedding_rows[chunk_id][1]
            )
        ),
    )
