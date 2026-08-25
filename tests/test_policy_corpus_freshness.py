"""Manifest/database freshness proof without document text or vectors."""
from libs.policy_corpus.freshness import check_corpus_freshness
from libs.policy_corpus.chunking import chunk_markdown
from libs.policy_corpus.manifest import load_ingestable_documents, load_manifest

MANIFEST = "docs/RagDocs/manifest.json"


def _expected_rows():
    manifest = load_manifest(MANIFEST)
    documents, embeddings = [], []
    for doc, text in load_ingestable_documents(MANIFEST):
        documents.append((doc.source_id, doc.source_version, doc.content_sha256))
        for chunk in chunk_markdown(
            source_id=doc.source_id,
            source_version=doc.source_version,
            markdown_text=text,
            config=manifest.ingestion.chunking,
        ):
            embeddings.append((chunk.chunk_id, chunk.chunk_hash, chunk.chunk_hash, 1024))
    return documents, embeddings


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.conn.queries.append((" ".join(sql.split()), params))
        self.rows = self.conn.documents if "FROM policy_documents" in sql and "JOIN" not in sql else self.conn.embeddings

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, documents, embeddings):
        self.documents = documents
        self.embeddings = embeddings
        self.queries = []
        self.rolled_back = False

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rolled_back = True


def test_current_manifest_and_matching_database_are_fresh():
    documents, embeddings = _expected_rows()
    conn = _Connection(documents, embeddings)

    report = check_corpus_freshness(conn, MANIFEST, provider="bedrock", model="titan-v2")

    assert report.is_fresh
    assert report.expected_documents == 15
    assert report.expected_chunks == len(embeddings)
    assert report.dimensions == (1024,)
    assert conn.rolled_back
    assert all("text" not in sql.lower() and "embedding," not in sql.lower() for sql, _ in conn.queries)


def test_freshness_detects_document_embedding_hash_and_dimension_drift():
    documents, embeddings = _expected_rows()
    changed_documents = list(documents)
    changed_documents[0] = (*changed_documents[0][:2], "wrong-document-hash")
    changed_embeddings = list(embeddings)
    chunk_id, chunk_hash, _, _ = changed_embeddings[0]
    changed_embeddings[0] = (chunk_id, "wrong-chunk-hash", "wrong-embedding-hash", 16)
    conn = _Connection(changed_documents, changed_embeddings)

    report = check_corpus_freshness(conn, MANIFEST, provider="bedrock", model="titan-v2")

    assert not report.is_fresh
    assert report.document_hash_mismatches
    assert report.chunk_hash_mismatches == (chunk_id,)
    assert report.embedding_hash_mismatches == (chunk_id,)
    assert 16 in report.dimensions


def test_freshness_detects_missing_and_extra_active_rows():
    documents, embeddings = _expected_rows()
    removed_document = f"{documents[0][0]}@{documents[0][1]}"
    removed_chunk = embeddings[0][0]
    conn = _Connection(
        documents[1:] + [("EXTRA", "1.0", "hash")],
        embeddings[1:] + [("EXTRA@1.0#x", "hash", "hash", 1024)],
    )

    report = check_corpus_freshness(conn, MANIFEST, provider="bedrock", model="titan-v2")

    assert removed_document in report.missing_documents
    assert "EXTRA@1.0" in report.extra_active_documents
    assert removed_chunk in report.missing_embeddings
    assert "EXTRA@1.0#x" in report.extra_active_embeddings
