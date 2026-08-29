"""Tests for the policy corpus's metadata-filtered retrieval interface
(libs/policy_corpus/retrieval.py), driven against a fake psycopg2-shaped
connection and a fake embedding client — no real Postgres or Bedrock
required. Real pgvector/audience-workflow filtering behavior against a live
database is exercised in tests/integration/test_policy_corpus_pipeline.py.
"""
from libs.policy_corpus.retrieval import PolicyRetriever, RetrievalLedger, RetrievalScope, RetrievedChunk


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), list(params or [])))

    def fetchall(self):
        return self._conn.rows_to_return


class _FakeConnection:
    def __init__(self, rows_to_return=None):
        self.executed = []
        self.rolled_back = False
        self.rows_to_return = rows_to_return or []

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.rolled_back = True


class _FakeEmbeddingClient:
    def __init__(self, vector=None):
        self.vector = vector or [0.1, 0.2, 0.3]
        self.embed_calls = []

    def embed(self, texts):
        self.embed_calls.append(list(texts))
        return [self.vector for _ in texts]


_ROW = ("POL-1", "1.0", "Test Policy", "2026-08-01", "overview", ["Overview"], "chunk text", 0.2)


# --- retrieve(): scope enforcement + query construction --------------------


def test_an_unscoped_query_returns_empty_without_ever_calling_the_embedding_client():
    embedding_client = _FakeEmbeddingClient()
    conn = _FakeConnection()
    retriever = PolicyRetriever(conn, embedding_client, provider="bedrock", model="titan-v2")

    result = retriever.retrieve("any query", RetrievalScope(audiences=(), workflows=("patient_summary",)), limit=5)

    assert result == []
    assert embedding_client.embed_calls == []
    assert conn.executed == []


def test_retrieve_filters_on_provider_model_audiences_and_workflows():
    embedding_client = _FakeEmbeddingClient()
    conn = _FakeConnection(rows_to_return=[_ROW])
    retriever = PolicyRetriever(conn, embedding_client, provider="bedrock", model="titan-v2")
    scope = RetrievalScope(audiences=("patient", "clinician"), workflows=("patient_summary",))

    retriever.retrieve("what's my coverage?", scope, limit=5)

    sql, params = conn.executed[0]
    assert "e.provider = %s AND e.model = %s" in sql
    assert "d.audiences && %s::text[] AND d.workflows && %s::text[]" in sql
    assert "d.synthetic AND d.retrieval_enabled AND d.approval_status = 'approved_training'" in sql
    assert params == [[0.1, 0.2, 0.3], "bedrock", "titan-v2", ["patient", "clinician"], ["patient_summary"], 5]


def test_retrieve_narrows_by_topic_only_when_given():
    embedding_client = _FakeEmbeddingClient()
    conn = _FakeConnection(rows_to_return=[])
    retriever = PolicyRetriever(conn, embedding_client, provider="bedrock", model="titan-v2")
    scope = RetrievalScope(audiences=("patient",), workflows=("patient_summary",), topic="laboratory_results")

    retriever.retrieve("query", scope, limit=3)

    sql, params = conn.executed[0]
    assert "%s = ANY(d.topics)" in sql
    assert "laboratory_results" in params


# --- SA-TOPIC-MISMATCH regression: patient-summary's untrusted-topic-free
# call shape ------------------------------------------------------------------


def test_summary_style_retrieval_with_no_trusted_topic_has_no_topic_predicate():
    """The patient-summary tool (libs/summary_agent/retrieval.py) never
    supplies a topic — this proves what PolicyRetriever itself does with
    that exact call shape: no `ANY(d.topics)` predicate is added at all,
    the same as any other caller that passes no topic. PolicyRetriever's
    own general topic capability is untouched (see the test above) — this
    only confirms the absence of one when the caller supplies none."""
    embedding_client = _FakeEmbeddingClient()
    conn = _FakeConnection(rows_to_return=[_ROW])
    retriever = PolicyRetriever(conn, embedding_client, provider="bedrock", model="titan-v2")
    scope = RetrievalScope(audiences=("patient",), workflows=("patient_summary",))

    retriever.retrieve("approved guidance for a patient-facing chart summary", scope, limit=3)

    sql, params = conn.executed[0]
    assert "ANY(d.topics)" not in sql
    assert "policy" not in params
    assert "training" not in params


def test_a_returned_approved_row_remains_retrievable_with_no_trusted_topic():
    embedding_client = _FakeEmbeddingClient()
    conn = _FakeConnection(rows_to_return=[_ROW])
    retriever = PolicyRetriever(conn, embedding_client, provider="bedrock", model="titan-v2")
    scope = RetrievalScope(audiences=("patient",), workflows=("patient_summary",))

    [chunk] = retriever.retrieve("approved guidance", scope, limit=3)

    assert chunk.citation_id == "POL-1@1.0#overview"
    assert chunk.text == "chunk text"


def test_retrieve_rolls_back_after_a_pure_read():
    embedding_client = _FakeEmbeddingClient()
    conn = _FakeConnection(rows_to_return=[])
    retriever = PolicyRetriever(conn, embedding_client, provider="bedrock", model="titan-v2")

    retriever.retrieve("query", RetrievalScope(audiences=("patient",), workflows=("patient_summary",)), limit=3)

    assert conn.rolled_back is True


def test_retrieve_maps_rows_to_retrieved_chunks_with_a_stable_citation_id():
    embedding_client = _FakeEmbeddingClient()
    conn = _FakeConnection(rows_to_return=[_ROW])
    retriever = PolicyRetriever(conn, embedding_client, provider="bedrock", model="titan-v2")

    [chunk] = retriever.retrieve(
        "query", RetrievalScope(audiences=("patient",), workflows=("patient_summary",)), limit=1
    )

    assert isinstance(chunk, RetrievedChunk)
    assert chunk.citation_id == "POL-1@1.0#overview"
    assert chunk.title == "Test Policy"
    assert chunk.heading_path == ("Overview",)
    assert chunk.text == "chunk text"
    assert chunk.score == 0.8  # 1.0 - distance(0.2)


# --- RetrievalLedger: reject a citation never actually retrieved -----------


def test_ledger_accepts_a_citation_that_was_retrieved():
    ledger = RetrievalLedger()
    chunk = RetrievedChunk(
        citation_id="POL-1@1.0#overview", source_id="POL-1", source_version="1.0", title="t",
        effective_date="2026-08-01", section_id="overview", heading_path=("Overview",), score=0.9, text="body",
    )
    ledger.record([chunk])

    assert ledger.is_valid_citation("POL-1@1.0#overview") is True


def test_ledger_rejects_a_citation_never_retrieved_for_this_request():
    ledger = RetrievalLedger()
    chunk = RetrievedChunk(
        citation_id="POL-1@1.0#overview", source_id="POL-1", source_version="1.0", title="t",
        effective_date="2026-08-01", section_id="overview", heading_path=("Overview",), score=0.9, text="body",
    )
    ledger.record([chunk])

    assert ledger.is_valid_citation("POL-2@1.0#other-section") is False


def test_ledger_starts_empty():
    ledger = RetrievalLedger()

    assert ledger.is_valid_citation("anything") is False
