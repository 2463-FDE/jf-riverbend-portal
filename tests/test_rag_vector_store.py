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

    def execute(self, sql, params=None):
        params = list(params) if params is not None else []
        self._conn.executed.append((" ".join(sql.split()), params))
        statement = sql.strip().upper()
        if statement.startswith("INSERT"):
            record_id, patient_id, provider, model, dimension, content_hash, embedding = params
            key = (record_id, provider, model)
            prior = self._conn.stored.get(key)
            # Mirrors the real ON CONFLICT ... WHERE predicate: skip only when
            # content_hash, dimension, AND patient_id are all unchanged.
            unchanged = (
                prior is not None
                and prior["content_hash"] == content_hash
                and prior["dimension"] == dimension
                and prior["patient_id"] == patient_id
            )
            if unchanged:
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
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _pgvector_store(dimension=3, provider="fake", model="", connection=None):
    return PgVectorStore(dimension=dimension, provider=provider, model=model, connection=connection or _FakeConnection())


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


def test_pgvector_store_derives_model_from_provider_when_not_given(monkeypatch):
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    store = PgVectorStore(dimension=3, provider="ollama", connection=_FakeConnection())
    assert store._model == "nomic-embed-text"


def test_pgvector_store_model_defaults_to_empty_for_the_fake_provider():
    store = PgVectorStore(dimension=3, provider="fake", connection=_FakeConnection())
    assert store._model == ""


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


def test_in_memory_store_reindexing_with_a_smaller_corpus_evicts_the_dropped_record():
    # Reviewer finding: index() must REPLACE the active corpus, not merge
    # into it — an append-only _corpus_by_id would keep r2 eligible forever
    # after a re-index that no longer includes it.
    store = InMemoryCosineStore()
    store.index([_record("r1", 1, "a"), _record("r2", 2, "b")], {"r1": [1.0, 0.0], "r2": [0.0, 1.0]})

    store.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})

    unscoped = store.retrieve_top_k([0.0, 1.0], k=5)
    assert "r2" not in {record.record_id for record in unscoped}

    scoped = store.retrieve_top_k([0.0, 1.0], k=5, patient_id=2)
    assert scoped == []  # r2 (patient 2) is gone even when queried by its own patient_id


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


def test_pgvector_store_reindexing_with_a_smaller_corpus_evicts_the_dropped_record():
    # Reviewer finding: index() must REPLACE the active corpus, not merge
    # into it. rag_embeddings intentionally keeps the OLD row for r2 in the
    # DB (migration 010) — record_id = ANY(:eligible_ids) is the only thing
    # keeping it unreachable, so an append-only _corpus_by_id would silently
    # widen that constraint back to include r2 on the very next
    # retrieve_top_k call, including a patient-scoped one for r2's own
    # patient_id. (The fake connection here doesn't simulate real SQL
    # filtering, so this checks the constraint that WOULD be sent — the real
    # filtering is proven against a live Postgres in the integration test.)
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 1, "a"), _record("r2", 2, "b")], {"r1": [1.0, 0.0], "r2": [0.0, 1.0]})

    store.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})

    assert set(store._corpus_by_id.keys()) == {"r1"}

    store._conn.select_response = [("r1",)]
    store.retrieve_top_k([1.0, 0.0], k=5, patient_id=2)  # patient_id of the now-evicted r2

    sql, params = store._conn.executed[-1]
    eligible_ids_param = params[2]
    assert "r2" not in eligible_ids_param
    assert eligible_ids_param == ["r1"]


def test_pgvector_store_reindexes_on_dimension_change_even_with_unchanged_text():
    # Reviewer's secondary ask: the rewrite predicate must not be
    # content_hash-only — a dimension change (e.g. a wider model) with
    # unchanged corpus text must still be treated as a real change.
    conn = _FakeConnection()
    same_text = "unchanged text"
    _pgvector_store(dimension=2, connection=conn).index([_record("r1", 1, same_text)], {"r1": [1.0, 0.0]})

    result = _pgvector_store(dimension=3, connection=conn).index([_record("r1", 1, same_text)], {"r1": [1.0, 0.0, 0.0]})

    assert result == IndexResult(written=1, skipped=0)


def test_pgvector_store_reindexes_on_patient_id_change_even_with_unchanged_text():
    conn = _FakeConnection()
    same_text = "unchanged text"
    _pgvector_store(dimension=2, connection=conn).index([_record("r1", 1, same_text)], {"r1": [1.0, 0.0]})

    result = _pgvector_store(dimension=2, connection=conn).index([_record("r1", 2, same_text)], {"r1": [1.0, 0.0]})

    assert result == IndexResult(written=1, skipped=0)


# --- PgVectorStore (fake connection): model is part of the embedding identity


def test_pgvector_store_a_model_swap_under_the_same_provider_writes_a_new_row_not_a_skip():
    # Reviewer finding: the old conflict key (record_id, provider) let a
    # model swap under the same provider (e.g. a different
    # OLLAMA_EMBED_MODEL) silently keep the OLD model's vector, since
    # content_hash (over the corpus TEXT, not the model) was unchanged.
    # `model` must be part of the identity so this is a fresh row, not a skip.
    conn = _FakeConnection()
    same_text = "unchanged text"
    model_a = _pgvector_store(dimension=2, provider="ollama", model="model-a", connection=conn)
    model_b = _pgvector_store(dimension=2, provider="ollama", model="model-b", connection=conn)

    first = model_a.index([_record("r1", 1, same_text)], {"r1": [1.0, 0.0]})
    second = model_b.index([_record("r1", 1, same_text)], {"r1": [0.0, 1.0]})

    assert first == IndexResult(written=1, skipped=0)
    assert second == IndexResult(written=1, skipped=0)  # NOT skipped — a distinct identity, not a duplicate
    assert conn.stored[("r1", "ollama", "model-a")]["embedding"] == [1.0, 0.0]
    assert conn.stored[("r1", "ollama", "model-b")]["embedding"] == [0.0, 1.0]


def test_pgvector_store_query_is_scoped_to_its_own_provider_and_model():
    # Two stores pointed at different models must never compare each other's
    # vectors — the retrieval filter must carry both provider AND model.
    conn = _FakeConnection()
    model_a = _pgvector_store(dimension=2, provider="ollama", model="model-a", connection=conn)
    model_b = _pgvector_store(dimension=2, provider="ollama", model="model-b", connection=conn)
    model_a.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})
    model_b.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})
    conn.select_response = [("r1",)]

    model_a.retrieve_top_k([1.0, 0.0], k=1)
    sql_a, params_a = conn.executed[-1]
    model_b.retrieve_top_k([1.0, 0.0], k=1)
    sql_b, params_b = conn.executed[-1]

    assert "model = %s" in sql_a and "model = %s" in sql_b
    assert "model-a" in params_a and "model-b" not in params_a
    assert "model-b" in params_b and "model-a" not in params_b


# --- PgVectorStore (fake connection): patient_id filter on the SQL layer ----


def test_pgvector_store_query_carries_no_patient_filter_when_unscoped():
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = [("r1",)]

    store.retrieve_top_k([1.0, 0.0], k=1)

    sql, params = store._conn.executed[-1]
    assert "patient_id" not in sql
    # provider, model, eligible_ids, query_vector, limit — no patient filter
    assert params == ["fake", "", ["r1"], [1.0, 0.0], 1]


# --- PgVectorStore (fake connection): constrained to the active corpus -----


def test_pgvector_store_query_constrains_to_this_instances_indexed_record_ids():
    # Reviewer finding: rag_embeddings never deletes rows on its own, so a
    # stale row from an older run could otherwise rank inside LIMIT and get
    # silently dropped. The SQL itself must restrict to this instance's
    # currently-indexed corpus, not filter after the fact.
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 1, "a"), _record("r2", 1, "b")], {"r1": [1.0, 0.0], "r2": [0.0, 1.0]})
    store._conn.select_response = [("r1",)]

    store.retrieve_top_k([1.0, 0.0], k=1)

    sql, params = store._conn.executed[-1]
    assert "record_id = ANY(%s)" in sql
    eligible_ids_param = params[2]
    assert set(eligible_ids_param) == {"r1", "r2"}


def test_pgvector_store_retrieve_on_a_store_with_nothing_indexed_returns_empty_without_querying():
    store = _pgvector_store(dimension=2)

    result = store.retrieve_top_k([1.0, 0.0], k=5)

    assert result == []
    assert store._conn.executed == []  # never even reaches the DB


def test_pgvector_store_raises_rather_than_silently_dropping_an_unexpected_record_id():
    # If the DB ever returned a record_id outside this instance's own
    # eligible_ids (it shouldn't, given the ANY() constraint above, but this
    # documents the contract), that must raise loudly, never be dropped.
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = [("some-other-id-not-in-this-corpus",)]

    with pytest.raises(KeyError):
        store.retrieve_top_k([1.0, 0.0], k=1)


# --- PgVectorStore (fake connection): SET LOCAL never leaks past a read ----


def test_pgvector_store_rolls_back_after_a_scoped_read():
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 7, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = [("r1",)]

    store.retrieve_top_k([1.0, 0.0], k=1, patient_id=7)

    assert store._conn.rolled_back is True


def test_pgvector_store_rolls_back_after_an_unscoped_read_too():
    # Reviewer finding: without this, a scoped read's SET LOCAL (disabled
    # indexes) would leak into every later read on the same connection,
    # including unscoped ones, since SET LOCAL lasts until end of
    # transaction, not end of cursor.
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = [("r1",)]

    store.retrieve_top_k([1.0, 0.0], k=1)

    assert store._conn.rolled_back is True


def test_pgvector_store_unscoped_query_does_not_force_an_exact_scan():
    # Unscoped queries (the eval harness's usage) keep using the ANN index —
    # only the security-scoped path pays the exact-scan cost.
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 1, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = [("r1",)]

    store.retrieve_top_k([1.0, 0.0], k=1)

    statements = [sql for sql, _ in store._conn.executed]
    assert not any("enable_indexscan" in sql for sql in statements)


def test_pgvector_store_query_filters_by_patient_id_when_scoped():
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 7, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = []  # no row for a different patient

    result = store.retrieve_top_k([1.0, 0.0], k=1, patient_id=7)

    sql, params = store._conn.executed[-1]
    assert "AND patient_id = %s" in sql
    assert 7 in params
    assert result == []  # fake DB returned nothing for this scope


def test_pgvector_store_scoped_query_forces_an_exact_scan():
    # Reviewer finding: HNSW applies WHERE patient_id = %s AFTER the
    # approximate graph search, so a filtered ORDER BY ... LIMIT k can
    # silently return fewer than k rows even when k+ eligible rows exist.
    # This is the security-scoped path, so it must disable index/bitmap scan
    # methods for that query rather than rely on the ANN index — verified
    # against a live pgvector 0.8.0 database that this reliably fixes it
    # where `hnsw.iterative_scan` did not (see vector_store.py's docstring).
    store = _pgvector_store(dimension=2)
    store.index([_record("r1", 7, "a")], {"r1": [1.0, 0.0]})
    store._conn.select_response = [("r1",)]

    store.retrieve_top_k([1.0, 0.0], k=1, patient_id=7)

    statements = [sql for sql, _ in store._conn.executed]
    select_index = next(i for i, sql in enumerate(statements) if sql.upper().startswith("SELECT"))
    set_local_statements = statements[:select_index]  # must run before the SELECT to take effect
    assert "SET LOCAL enable_indexscan = off" in set_local_statements
    assert "SET LOCAL enable_bitmapscan = off" in set_local_statements
    assert "SET LOCAL enable_indexonlyscan = off" in set_local_statements


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


# --- harness wiring: the vector store must reflect the ACTIVE embedding provider


def test_run_eval_wires_the_active_embedding_provider_into_the_vector_store(monkeypatch, tmp_path):
    # Reviewer finding: run_eval() must not let build_vector_store() default
    # to "fake" regardless of the actual EMBEDDING_PROVIDER — a real
    # EMBEDDING_PROVIDER=ollama + RAG_VECTOR_STORE=pgvector run would
    # otherwise persist/query vectors mislabeled as "fake", or fail with a
    # dimension mismatch attributed to the wrong provider.
    from libs.rag_eval import harness

    captured = {}

    def spy_build_vector_store(name=None, **kwargs):
        captured["kwargs"] = kwargs
        return InMemoryCosineStore()  # avoid needing a real Postgres for this test

    monkeypatch.setattr(harness, "build_vector_store", spy_build_vector_store)

    # provider_name reports "ollama" (what a real EMBEDDING_PROVIDER=ollama
    # config would report) while the actual embedding object stays the
    # deterministic fake provider — this test is about the WIRING (what
    # build_vector_store gets called with), not a real Ollama network call.
    # RAG_VECTOR_STORE itself is resolved inside the real build_vector_store
    # (bypassed here by the spy), so it's not this test's concern.
    embedding_client = EmbeddingClient(config=EmbeddingConfig(provider="ollama"), provider=FakeEmbeddingProvider())
    config = CorpusConfig(max_records=2, cache_dir=str(tmp_path))

    harness.run_eval(corpus_config=config, embedding_client=embedding_client)

    assert captured["kwargs"].get("provider") == "ollama"


def test_run_eval_still_wires_fake_when_that_is_the_active_provider(monkeypatch, tmp_path):
    from libs.rag_eval import harness

    captured = {}

    def spy_build_vector_store(name=None, **kwargs):
        captured["kwargs"] = kwargs
        return InMemoryCosineStore()

    monkeypatch.setattr(harness, "build_vector_store", spy_build_vector_store)
    config = CorpusConfig(max_records=2, cache_dir=str(tmp_path))

    harness.run_eval(corpus_config=config, embedding_client=_client())

    assert captured["kwargs"].get("provider") == "fake"
