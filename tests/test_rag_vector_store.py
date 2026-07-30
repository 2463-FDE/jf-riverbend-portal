"""Tests for the swappable retrieval interface (libs/rag_corpus/vector_store.py):
the fail-closed factory, the in-memory cosine store (the no-infra default),
and PgVectorStore driven entirely against a fake, dependency-injected
connection — no real Postgres and no pgvector package required. Real
pgvector behavior (a genuine ANN query, a genuine empty cross-patient
result) is exercised separately in
tests/integration/test_pgvector_retrieval.py against a live `make up` stack.
"""
import logging

import pytest

from libs.rag_corpus import CorpusConfig, CorpusRecord, InMemoryCosineStore, PgVectorStore, build_vector_store
from libs.rag_corpus.pipeline import run_pipeline
from libs.rag_corpus.vector_store import IndexResult, VectorStore
from libs.embedding_client import EmbeddingClient, EmbeddingConfig
from libs.embedding_client.providers.fake_provider import FakeEmbeddingProvider

PHI_MARKER = "ssn=111-22-3333"


def _record(record_id, patient_id, text="fixed fixture text"):
    return CorpusRecord(
        record_id=record_id,
        patient_id=patient_id,
        patient_name="seed fixture",
        text=text,
        occurred_at="2026-01-01 00:00:00",
    )


def _log_text(caplog):
    return "\n".join(record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- #
# Fake psycopg2-shaped connection — drives PgVectorStore with no real
# Postgres and no pgvector package installed.
# --------------------------------------------------------------------------- #


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params):
        params = list(params)
        self._conn.executed.append((" ".join(sql.split()), params))
        statement = sql.strip().upper()
        if statement.startswith("INSERT"):
            record_id, patient_id, provider, dimension, content_hash, embedding = params
            key = (record_id, provider)
            prior = self._conn.stored.get(key)
            if prior is not None and prior["content_hash"] == content_hash:
                self.rowcount = 0
            else:
                self.rowcount = 1
                self._conn.stored[key] = {
                    "patient_id": patient_id,
                    "dimension": dimension,
                    "content_hash": content_hash,
                    "embedding": embedding,
                }
        elif statement.startswith("SELECT"):
            self._rows = self._conn.select_response

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self):
        self.executed = []
        self.stored = {}
        self.select_response = []
        self.committed = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True


def _pgvector_store(dimension=3, provider="fake"):
    return PgVectorStore(dimension=dimension, provider=provider, connection=_FakeConnection())


# --- factory: fail-closed, config-only selection -----------------------------


def test_build_vector_store_defaults_to_in_memory(monkeypatch):
    monkeypatch.delenv("RAG_VECTOR_STORE", raising=False)
    assert isinstance(build_vector_store(), InMemoryCosineStore)


def test_build_vector_store_rejects_unknown_name():
    with pytest.raises(ValueError, match="Unknown RAG_VECTOR_STORE"):
        build_vector_store("chroma")


def test_build_vector_store_fails_closed_on_unrecognized_env_value(monkeypatch):
    monkeypatch.setenv("RAG_VECTOR_STORE", "some-typo")
    with pytest.raises(ValueError, match="Unknown RAG_VECTOR_STORE"):
        build_vector_store()


def test_build_vector_store_pgvector_never_imports_psycopg2_when_connection_injected():
    # Proves the lazy-import contract: requesting "pgvector" with an injected
    # connection must not require psycopg2 or the pgvector package at all.
    store = build_vector_store("pgvector", connection=_FakeConnection(), dimension=3)
    assert isinstance(store, PgVectorStore)


# --- InMemoryCosineStore: ranking + patient-scope boundary -------------------


def test_in_memory_store_ranks_by_cosine_similarity():
    store = InMemoryCosineStore()
    corpus = [_record("r1", 1, "a"), _record("r2", 1, "b")]
    vectors = {"r1": [1.0, 0.0], "r2": [0.0, 1.0]}
    store.index(corpus, vectors)

    result = store.retrieve_top_k([1.0, 0.0], k=1)

    assert [record.record_id for record in result] == ["r1"]


def test_in_memory_store_unscoped_query_searches_the_whole_corpus():
    store = InMemoryCosineStore()
    corpus = [_record("r1", 1, "a"), _record("r2", 2, "b")]
    vectors = {"r1": [1.0, 0.0], "r2": [0.9, 0.1]}
    store.index(corpus, vectors)

    result = store.retrieve_top_k([1.0, 0.0], k=2)

    assert {record.record_id for record in result} == {"r1", "r2"}


def test_in_memory_store_patient_scope_excludes_a_better_match_from_another_patient():
    # The retrieval analogue of the RIV-201 boundary: patient 2's record is
    # the objectively closer match, but a query scoped to patient 1 must
    # never return it.
    store = InMemoryCosineStore()
    corpus = [_record("r1", 1, "a"), _record("r2", 2, "b")]
    vectors = {"r1": [0.5, 0.5], "r2": [1.0, 0.0]}
    store.index(corpus, vectors)

    result = store.retrieve_top_k([1.0, 0.0], k=2, patient_id=1)

    assert [record.record_id for record in result] == ["r1"]


def test_in_memory_store_cross_patient_query_with_no_records_returns_empty():
    store = InMemoryCosineStore()
    store.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})

    result = store.retrieve_top_k([1.0, 0.0], k=5, patient_id=999)

    assert result == []


def test_in_memory_store_does_not_log_record_text_or_vectors(caplog):
    caplog.set_level(logging.INFO)
    store = InMemoryCosineStore()
    text = f"chart note containing {PHI_MARKER}"
    store.index([_record("r1", 1, text)], {"r1": [1.0, 0.0]})
    store.retrieve_top_k([1.0, 0.0], k=1)

    log_text = _log_text(caplog)
    assert PHI_MARKER not in log_text
    assert text not in log_text


# --- PgVectorStore (fake connection): dimension check ------------------------


def test_pgvector_store_rejects_dimension_mismatch_on_index():
    store = _pgvector_store(dimension=3)
    with pytest.raises(ValueError, match="dimension mismatch"):
        store.index([_record("r1", 1)], {"r1": [1.0, 0.0]})  # 2 dims, not 3


def test_pgvector_store_rejects_dimension_mismatch_on_query():
    store = _pgvector_store(dimension=3)
    with pytest.raises(ValueError, match="dimension mismatch"):
        store.retrieve_top_k([1.0, 0.0], k=1)


# --- PgVectorStore (fake connection): idempotent persistence -----------------


def test_pgvector_store_second_index_over_unchanged_records_writes_nothing():
    store = _pgvector_store(dimension=2)
    corpus = [_record("r1", 1, "version one"), _record("r2", 2, "stable text")]
    vectors = {"r1": [1.0, 0.0], "r2": [0.0, 1.0]}

    first = store.index(corpus, vectors)
    assert first == IndexResult(written=2, skipped=0)

    second = store.index(corpus, vectors)
    assert second == IndexResult(written=0, skipped=2)


def test_pgvector_store_only_reindexes_records_whose_text_changed():
    store = _pgvector_store(dimension=2)
    corpus_v1 = [_record("r1", 1, "version one"), _record("r2", 2, "stable text")]
    vectors_v1 = {"r1": [1.0, 0.0], "r2": [0.0, 1.0]}
    store.index(corpus_v1, vectors_v1)

    corpus_v2 = [_record("r1", 1, "version two — changed"), _record("r2", 2, "stable text")]
    vectors_v2 = {"r1": [0.5, 0.5], "r2": [0.0, 1.0]}
    result = store.index(corpus_v2, vectors_v2)

    assert result == IndexResult(written=1, skipped=1)


# --- PgVectorStore (fake connection): patient_id filter on the SQL layer ----


def test_pgvector_store_query_carries_no_patient_filter_when_unscoped():
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = [("r1",)]

    store.retrieve_top_k([1.0, 0.0], k=1)

    sql, params = store._conn.executed[-1]
    assert "patient_id" not in sql
    assert params == ["fake", [1.0, 0.0], 1]  # provider, query_vector, limit — no patient filter param


def test_pgvector_store_query_filters_by_patient_id_when_scoped():
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 7, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = []  # no row for a different patient

    result = store.retrieve_top_k([1.0, 0.0], k=1, patient_id=7)

    sql, params = store._conn.executed[-1]
    assert "AND patient_id = %s" in sql
    assert 7 in params
    assert result == []  # fake DB returned nothing for this scope


def test_pgvector_store_maps_returned_record_ids_back_to_corpus_records():
    store = _pgvector_store(dimension=2)
    r1, r2 = _record("r1", 1, "a"), _record("r2", 1, "b")
    store.index([r1, r2], {"r1": [1.0, 0.0], "r2": [0.0, 1.0]})
    store._conn.select_response = [("r2",), ("r1",)]  # DB decides the order

    result = store.retrieve_top_k([0.0, 1.0], k=2)

    assert result == [r2, r1]


def test_pgvector_store_does_not_log_record_text_or_vectors(caplog):
    caplog.set_level(logging.INFO)
    store = _pgvector_store(dimension=2)
    text = f"chart note containing {PHI_MARKER}"
    store.index([_record("r1", 1, text)], {"r1": [1.0, 0.0]})
    store._conn.select_response = [("r1",)]
    store.retrieve_top_k([1.0, 0.0], k=1)

    log_text = _log_text(caplog)
    assert PHI_MARKER not in log_text
    assert text not in log_text
    assert "1.0" not in log_text


# --- pipeline persistence seam: opt-in, backward compatible ------------------


class _SpyVectorStore(VectorStore):
    def __init__(self):
        self.indexed = None

    def index(self, corpus, vectors_by_record_id):
        self.indexed = (corpus, dict(vectors_by_record_id))
        return IndexResult(written=len(corpus), skipped=0)

    def retrieve_top_k(self, query_vector, k, *, patient_id=None):
        raise NotImplementedError


def _client():
    return EmbeddingClient(config=EmbeddingConfig(provider="fake"), provider=FakeEmbeddingProvider())


def test_run_pipeline_without_a_vector_store_behaves_exactly_as_before(tmp_path):
    config = CorpusConfig(max_records=2, cache_dir=str(tmp_path))
    result = run_pipeline(config=config, embedding_client=_client())
    assert result.index_result is None


def test_run_pipeline_indexes_into_the_given_vector_store(tmp_path):
    config = CorpusConfig(max_records=2, cache_dir=str(tmp_path))
    store = _SpyVectorStore()

    result = run_pipeline(config=config, embedding_client=_client(), vector_store=store)

    assert result.index_result == IndexResult(written=len(result.corpus), skipped=0)
    indexed_corpus, indexed_vectors = store.indexed
    assert indexed_corpus == result.corpus
    assert indexed_vectors == result.vectors_by_record_id
