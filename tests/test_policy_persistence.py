"""Tests for idempotent policy corpus persistence
(libs/policy_corpus/persistence.py), driven entirely against a fake
psycopg2-shaped connection — no real Postgres required. Mirrors
tests/test_rag_vector_store.py's fake-connection approach; real pgvector
behavior against a live database belongs in
tests/integration/test_policy_corpus_pipeline.py.
"""
import hashlib
import json

import pytest

from libs.policy_corpus.contracts import PolicyChunk, PolicyDocumentMeta
from libs.policy_corpus.persistence import (
    DimensionMismatchError,
    EmbeddingCountMismatchError,
    existing_embedding_hashes,
    ingest_corpus,
    upsert_chunks,
    upsert_document,
    upsert_embedding,
)


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self._rows = []
        self._returning = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        params = list(params) if params is not None else []
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))

        if "INSERT INTO policy_documents" in normalized:
            source_id, source_version = params[1], params[2]
            key = (source_id, source_version)
            existing = self._conn.documents.get(key)
            row_id = existing["id"] if existing else self._conn.next_document_id
            if existing is None:
                self._conn.next_document_id += 1
            self._conn.documents[key] = {"id": row_id, "params": params, "retrieval_enabled": params[8]}
            self.rowcount = 1
            self._returning = (row_id,)

        elif normalized.startswith("UPDATE policy_documents SET retrieval_enabled = false"):
            current = set(zip(*params)) if params else None
            count = 0
            for key, row in self._conn.documents.items():
                if row["retrieval_enabled"] and (current is None or key not in current):
                    row["retrieval_enabled"] = False
                    count += 1
            self.rowcount = count

        elif "INSERT INTO policy_chunks" in normalized:
            chunk_id, document_id, section_id, heading_path, text, chunk_hash, char_count = params
            prior = self._conn.chunks.get(chunk_id)
            unchanged = prior is not None and prior["chunk_hash"] == chunk_hash
            self.rowcount = 0 if unchanged else 1
            if not unchanged:
                self._conn.chunks[chunk_id] = {
                    "document_id": document_id, "text": text, "chunk_hash": chunk_hash,
                }

        elif "INSERT INTO policy_chunk_embeddings" in normalized:
            chunk_id, provider, model, dimension, content_hash, embedding = params
            self._conn.embeddings[(chunk_id, provider, model)] = {
                "dimension": dimension, "content_hash": content_hash, "embedding": embedding,
            }
            self.rowcount = 1

        elif normalized.startswith("SELECT chunk_id, content_hash"):
            provider, model, chunk_ids = params
            self._rows = [
                (cid, row["content_hash"])
                for (cid, p, m), row in self._conn.embeddings.items()
                if p == provider and m == model and cid in chunk_ids
            ]

    def fetchone(self):
        return self._returning

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self):
        self.executed = []
        self.documents = {}
        self.chunks = {}
        self.embeddings = {}
        self.next_document_id = 1
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _doc(source_id="POL-1", source_version="1.0", **overrides):
    fields = dict(
        source_id=source_id, source_version=source_version, effective_date="2026-08-01",
        title="Test Policy", owner="Owner", approval_status="approved_training",
        synthetic=True, retrieval_enabled=True, content_path="policy.md",
        content_sha256="abc123", audiences=("patient",), workflows=("patient_summary",),
        topics=("testing",), allowed_uses=("training_grounding",), prohibited_uses=(),
        relationships=(),
    )
    fields.update(overrides)
    return PolicyDocumentMeta(**fields)


def _chunk(chunk_id="POL-1@1.0#overview", chunk_hash="hash-a", text="body"):
    return PolicyChunk(
        chunk_id=chunk_id, source_id="POL-1", source_version="1.0", section_id="overview",
        heading_path=("Overview",), text=text, chunk_hash=chunk_hash,
    )


# --- upsert_document -----------------------------------------------------


def test_upsert_document_returns_a_stable_id_across_calls():
    conn = _FakeConnection()
    first_id = upsert_document(conn, _doc(), corpus_id="test-corpus")
    second_id = upsert_document(conn, _doc(title="Corrected Title"), corpus_id="test-corpus")

    assert first_id == second_id


# --- upsert_chunks: idempotent by content hash ---------------------------


def test_upsert_chunks_writes_a_new_chunk():
    conn = _FakeConnection()
    written, skipped = upsert_chunks(conn, 1, [_chunk()])

    assert (written, skipped) == (1, 0)


def test_upsert_chunks_skips_an_unchanged_chunk_on_reingestion():
    conn = _FakeConnection()
    upsert_chunks(conn, 1, [_chunk(chunk_hash="hash-a")])

    written, skipped = upsert_chunks(conn, 1, [_chunk(chunk_hash="hash-a")])

    assert (written, skipped) == (0, 1)


def test_upsert_chunks_rewrites_a_changed_chunk():
    conn = _FakeConnection()
    upsert_chunks(conn, 1, [_chunk(chunk_hash="hash-a", text="old body")])

    written, skipped = upsert_chunks(conn, 1, [_chunk(chunk_hash="hash-b", text="new body")])

    assert (written, skipped) == (1, 0)
    assert conn.chunks["POL-1@1.0#overview"]["text"] == "new body"


# --- upsert_embedding: dimension enforcement ------------------------------


def test_upsert_embedding_persists_a_vector():
    conn = _FakeConnection()
    upsert_embedding(
        conn, chunk_id="POL-1@1.0#overview", provider="bedrock", model="titan-v2",
        vector=[0.1, 0.2, 0.3], chunk_text="body",
    )

    stored = conn.embeddings[("POL-1@1.0#overview", "bedrock", "titan-v2")]
    assert stored["dimension"] == 3


def test_upsert_embedding_rejects_a_dimension_mismatch_before_touching_the_connection():
    conn = _FakeConnection()

    with pytest.raises(DimensionMismatchError, match="expected dimension 1024"):
        upsert_embedding(
            conn, chunk_id="POL-1@1.0#overview", provider="bedrock", model="titan-v2",
            vector=[0.1, 0.2, 0.3], chunk_text="body", expected_dimension=1024,
        )
    assert conn.executed == []  # rejected before any SQL ran


# --- existing_embedding_hashes: the skip-unless-changed lookup -----------


def test_existing_embedding_hashes_returns_only_the_requested_provider_and_model():
    conn = _FakeConnection()
    upsert_embedding(conn, chunk_id="c1", provider="bedrock", model="titan-v2", vector=[0.1], chunk_text="a")
    upsert_embedding(conn, chunk_id="c1", provider="fake", model="", vector=[0.2], chunk_text="a")

    result = existing_embedding_hashes(conn, ["c1"], provider="bedrock", model="titan-v2")

    assert list(result.keys()) == ["c1"]


def test_existing_embedding_hashes_of_an_empty_chunk_id_list_never_touches_the_connection():
    conn = _FakeConnection()

    result = existing_embedding_hashes(conn, [], provider="bedrock", model="titan-v2")

    assert result == {}
    assert conn.executed == []


# --- ingest_corpus: STALE-RETRIEVAL and EMBED-COUNT-MISMATCH review fixes --


def _write_manifest(tmp_path, doc_ids):
    docs = []
    for source_id in doc_ids:
        text = f"# {source_id}\nbody text for {source_id}"
        (tmp_path / f"{source_id}.md").write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        docs.append(
            {
                "source_id": source_id, "source_version": "1.0", "effective_date": "2026-08-01",
                "title": source_id, "owner": "Owner", "approval_status": "approved_training",
                "synthetic": True, "retrieval_enabled": True, "content_path": f"{source_id}.md",
                "content_sha256": digest, "audiences": ["patient"], "workflows": ["patient_summary"],
                "topics": [], "allowed_uses": [], "prohibited_uses": [], "relationships": [],
            }
        )
    manifest = {
        "schema_version": 1, "corpus_id": "test-corpus", "notice": "SYNTHETIC.",
        "ingestion": {
            "content_root": ".", "allowed_extensions": [".md"], "encoding": "utf-8",
            "max_document_bytes": 20000, "max_documents": 32,
            "chunking": {
                "strategy": "markdown_heading_sections", "max_characters": 1200,
                "overlap_characters": 120, "minimum_characters": 80, "preserve_heading_path": True,
            },
            "required_chunk_metadata": ["source_id", "source_version", "section_id"],
        },
        "documents": docs,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return str(manifest_path)


class _FakeEmbeddingClient:
    def __init__(self, vector_count_offset=0):
        self._offset = vector_count_offset

    def embed(self, texts):
        return [[0.1, 0.2] for _ in texts][: len(texts) + self._offset]


def test_ingest_corpus_deactivates_a_document_dropped_from_the_manifest(tmp_path):
    manifest_path = _write_manifest(tmp_path, ["POL-A", "POL-B"])
    conn = _FakeConnection()
    ingest_corpus(conn, manifest_path, _FakeEmbeddingClient(), provider="bedrock", model="titan-v2")
    assert conn.documents[("POL-A", "1.0")]["retrieval_enabled"] is True
    assert conn.documents[("POL-B", "1.0")]["retrieval_enabled"] is True

    manifest_path_v2 = _write_manifest(tmp_path, ["POL-A"])  # POL-B dropped
    report = ingest_corpus(conn, manifest_path_v2, _FakeEmbeddingClient(), provider="bedrock", model="titan-v2")

    assert report.documents_deactivated == 1
    assert conn.documents[("POL-A", "1.0")]["retrieval_enabled"] is True  # untouched
    assert conn.documents[("POL-B", "1.0")]["retrieval_enabled"] is False  # deactivated, not deleted
    assert ("POL-B", "1.0") in conn.documents  # still present — audit history preserved


def test_ingest_corpus_raises_on_embedding_count_mismatch_and_rolls_back(tmp_path):
    manifest_path = _write_manifest(tmp_path, ["POL-A"])
    conn = _FakeConnection()

    with pytest.raises(EmbeddingCountMismatchError):
        ingest_corpus(conn, manifest_path, _FakeEmbeddingClient(vector_count_offset=-1), provider="bedrock", model="titan-v2")

    assert conn.committed is False
    assert conn.rolled_back is True
