"""
Integration test for Stage 2's pgvector persistence + retrieval path
(libs/rag_corpus/vector_store.py::PgVectorStore) — requires the full stack up
(`make up`, on a *fresh* volume so db/schema.sql's rag_embeddings table and
`CREATE EXTENSION vector` are actually present; docker-compose.yml's postgres
image is pgvector/pgvector:0.8.6-pg15 specifically so that extension exists).

Connects to Postgres directly (not through a service, and not through the
gateway/records IDOR path — this deliverable adds no HTTP route) to drive
PgVectorStore against a real pgvector installation, proving what
tests/test_rag_vector_store.py's fake connection cannot: a genuine ANN query
and a genuine empty cross-patient result.

Requires the optional libs/rag_corpus/requirements.txt (pgvector, psycopg2)
installed; skipped, not failed, if they aren't.

Run with:  pytest -m integration tests/integration/test_pgvector_retrieval.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
pgvector_pkg = pytest.importorskip("pgvector")
from pgvector import Vector  # noqa: E402

from libs.embedding_client import EmbeddingClient, EmbeddingConfig  # noqa: E402
from libs.embedding_client.providers.fake_provider import FakeEmbeddingProvider  # noqa: E402
from libs.rag_corpus import CorpusConfig, CorpusRecord, InMemoryCosineStore, PgVectorStore, VectorStoreConfig  # noqa: E402
from libs.rag_corpus.pipeline import run_pipeline  # noqa: E402

pytestmark = pytest.mark.integration

_PROVIDER = "fake"
_DIMENSION = 16  # matches FakeEmbeddingProvider and db/migrations/010_pgvector_embeddings.sql's vector(16)

_DB_CONFIG = VectorStoreConfig(
    store="pgvector",
    # DB_HOST/etc default to "postgres" (the in-network hostname) everywhere
    # else in this repo; this test runs on the host, so default to localhost
    # instead — mirrors GATEWAY_URL's localhost default in the other
    # integration tests. docker-compose.yml publishes 5432 to the host.
    db_host=os.getenv("DB_HOST", "localhost"),
    db_port=os.getenv("DB_PORT", "5432"),
    db_name=os.getenv("DB_NAME", "riverbend"),
    db_user=os.getenv("DB_USER", "riverbend_app"),
    db_password=os.getenv("DB_PASSWORD", "changeme"),
)


def _raw_connection():
    return psycopg2.connect(
        host=_DB_CONFIG.db_host,
        port=_DB_CONFIG.db_port,
        dbname=_DB_CONFIG.db_name,
        user=_DB_CONFIG.db_user,
        password=_DB_CONFIG.db_password,
    )


@pytest.fixture(autouse=True)
def _clean_rag_embeddings():
    # Deterministic corpus + FakeEmbeddingProvider means every test in this
    # file computes the same record_ids/content_hashes/vectors — clear the
    # whole table first so each test's "first" index() call is genuinely
    # first, not a no-op skip left over from a previous test.
    conn = _raw_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rag_embeddings")
    conn.commit()
    conn.close()
    yield


def _embedding_client():
    return EmbeddingClient(config=EmbeddingConfig(provider=_PROVIDER), provider=FakeEmbeddingProvider())


def _run_pipeline_against_pgvector(tmp_path):
    config = CorpusConfig(cache_dir=str(tmp_path))  # full seed-fixture corpus (5 encounters, >1 patient)
    store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    result = run_pipeline(config=config, embedding_client=_embedding_client(), vector_store=store)
    return result, store


def test_migration_010_extension_and_table_are_present():
    store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    with store._conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert cur.fetchone() is not None, "CREATE EXTENSION vector from migration 010 is missing"
        cur.execute("SELECT to_regclass('rag_embeddings') IS NOT NULL")
        assert cur.fetchone()[0] is True, "rag_embeddings table from migration 010 is missing"


def test_second_pipeline_run_over_the_same_corpus_writes_nothing_new(tmp_path):
    first_result, _ = _run_pipeline_against_pgvector(tmp_path)
    assert first_result.index_result.written == len(first_result.corpus)
    assert first_result.index_result.skipped == 0

    second_result, _ = _run_pipeline_against_pgvector(tmp_path)
    assert second_result.index_result.written == 0
    assert second_result.index_result.skipped == len(second_result.corpus)


def test_pgvector_retrieval_matches_in_memory_cosine_ranking(tmp_path):
    pipeline_result, pg_store = _run_pipeline_against_pgvector(tmp_path)

    memory_store = InMemoryCosineStore()
    memory_store.index(pipeline_result.corpus, pipeline_result.vectors_by_record_id)

    query_record = pipeline_result.corpus[0]
    query_vector = pipeline_result.vectors_by_record_id[query_record.record_id]

    pgvector_top = [r.record_id for r in pg_store.retrieve_top_k(query_vector, k=3)]
    memory_top = [r.record_id for r in memory_store.retrieve_top_k(query_vector, k=3)]

    assert pgvector_top == memory_top


def test_pgvector_cross_patient_query_returns_nothing(tmp_path):
    pipeline_result, pg_store = _run_pipeline_against_pgvector(tmp_path)
    corpus = pipeline_result.corpus
    patient_ids = {record.patient_id for record in corpus}
    assert len(patient_ids) > 1, "fixture corpus must span more than one patient for this test to mean anything"

    target = corpus[0]
    other_patient_id = next(pid for pid in patient_ids if pid != target.patient_id)
    # target's own vector is by construction its own closest match — proves
    # a genuinely relevant record is still excluded once scoped elsewhere.
    query_vector = pipeline_result.vectors_by_record_id[target.record_id]

    scoped_result = pg_store.retrieve_top_k(query_vector, k=len(corpus), patient_id=other_patient_id)

    assert target.record_id not in {record.record_id for record in scoped_result}
    assert all(record.patient_id == other_patient_id for record in scoped_result)


def test_pgvector_query_scoped_to_a_patient_with_no_rows_returns_empty(tmp_path):
    pipeline_result, pg_store = _run_pipeline_against_pgvector(tmp_path)
    query_vector = pipeline_result.vectors_by_record_id[pipeline_result.corpus[0].record_id]

    result = pg_store.retrieve_top_k(query_vector, k=5, patient_id=-1)

    assert result == []


def test_pgvector_scoped_query_plan_never_uses_an_approximate_index():
    # Regression test for the PR #14 review finding, tested at the mechanism
    # level rather than by hoping a particular data distribution reproduces
    # it: pgvector's HNSW index applies `WHERE patient_id = %s` AFTER the
    # approximate graph search, so a selective patient filter combined with
    # `ORDER BY embedding <=> %s LIMIT k` CAN silently return fewer than k
    # rows even when k+ eligible rows exist — reproduced by hand against this
    # exact database (0 of 5 true matches returned) before this fix existed.
    # `rag_embeddings_patient_id_idx` (a plain B-tree) often gives the
    # planner an exact-and-fast escape hatch on its own for a small,
    # per-patient result set, but that is an incidental property of table
    # statistics, not a guarantee — this asserts the actual mechanism
    # (retrieve_top_k's SET LOCAL enable_indexscan/enable_bitmapscan/
    # enable_indexonlyscan = off for scoped queries) is what makes
    # correctness independent of the planner's discretion: EXPLAIN must show
    # no index scan of any kind (B-tree included) for a scoped query.
    pg_store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    pg_store.index(
        [CorpusRecord(record_id="r1", patient_id=1042, patient_name="x", text="t", occurred_at="2026-01-01")],
        {"r1": [1.0] + [0.0] * (_DIMENSION - 1)},
    )

    with pg_store._conn.cursor() as cur:
        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute("SET LOCAL enable_bitmapscan = off")
        cur.execute("SET LOCAL enable_indexonlyscan = off")
        cur.execute(
            "EXPLAIN SELECT record_id FROM rag_embeddings WHERE provider = %s AND model = %s "
            "AND record_id = ANY(%s) AND patient_id = %s ORDER BY embedding <=> %s LIMIT %s",
            [_PROVIDER, "", ["r1"], 1042, Vector([1.0] + [0.0] * (_DIMENSION - 1)), 5],
        )
        plan = "\n".join(row[0] for row in cur.fetchall())

    assert "Index Scan" not in plan and "Bitmap" not in plan, plan
    assert "Seq Scan" in plan, plan


def test_pgvector_unscoped_query_plan_also_forces_an_exact_scan():
    # Confirms the mechanism against a real planner: manually issues the same
    # SET LOCAL + query shape retrieve_top_k now sends unconditionally, and
    # checks the resulting plan avoids index/bitmap scans even when unscoped.
    # This does NOT, by itself, prove retrieve_top_k actually issues these
    # statements for an unscoped call — that's what
    # tests/test_rag_vector_store.py::
    # test_pgvector_store_unscoped_query_also_forces_an_exact_scan proves
    # (against a fake connection that records exactly what SQL was sent),
    # and is the test that actually catches a regression here: reverting the
    # fix and re-running the "at scale" test below did NOT fail, because the
    # table's own UNIQUE(record_id, provider, model) constraint gives the
    # planner an incidental exact-lookup index for a small eligible_ids list
    # regardless of this fix (the same phenomenon as the round-1
    # patient_id-B-tree escape hatch) — a reminder that a mechanism test
    # (this one, and the unit-level one) is what actually guards the fix;
    # the "at scale" test below is a correctness demonstration, not a
    # reliable regression discriminator.
    pg_store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    pg_store.index(
        [CorpusRecord(record_id="r1", patient_id=1042, patient_name="x", text="t", occurred_at="2026-01-01")],
        {"r1": [1.0] + [0.0] * (_DIMENSION - 1)},
    )

    with pg_store._conn.cursor() as cur:
        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute("SET LOCAL enable_bitmapscan = off")
        cur.execute("SET LOCAL enable_indexonlyscan = off")
        cur.execute(
            "EXPLAIN SELECT record_id FROM rag_embeddings WHERE provider = %s AND model = %s "
            "AND record_id = ANY(%s) ORDER BY embedding <=> %s LIMIT %s",
            [_PROVIDER, "", ["r1"], Vector([1.0] + [0.0] * (_DIMENSION - 1)), 5],
        )
        plan = "\n".join(row[0] for row in cur.fetchall())

    assert "Index Scan" not in plan and "Bitmap" not in plan, plan
    assert "Seq Scan" in plan, plan


def test_pgvector_unscoped_retrieval_returns_all_eligible_rows_despite_ineligible_noise_at_scale():
    # The reviewer's explicit ask: seed many closer INELIGIBLE rows (not in
    # this instance's _corpus_by_id — e.g. stale rows from an older run,
    # exactly what rag_embeddings intentionally keeps per migration 010) and
    # prove an UNSCOPED query still returns every eligible row despite them,
    # mirroring the scoped "at scale" test above but for the unscoped path
    # the eval harness actually uses. This is a correctness demonstration,
    # not a strict regression discriminator: the table's own
    # UNIQUE(record_id, provider, model) constraint gives the planner a cheap
    # exact-lookup path for a small eligible_ids list independent of this
    # fix — see the mechanism test above for what actually guards the fix.
    query_vector = [1.0] + [0.0] * (_DIMENSION - 1)

    pg_store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    target_corpus = [
        CorpusRecord(
            record_id=f"unscoped-target-{i:04d}",
            patient_id=1042,
            patient_name="x",
            text=f"target record {i}",
            occurred_at="2026-01-01",
        )
        for i in range(5)
    ]
    # far from the query (orthogonal direction) — the only eligible rows
    target_vectors = {
        record.record_id: [0.0] * (_DIMENSION - 1) + [1.0 + i * 0.01] for i, record in enumerate(target_corpus)
    }
    pg_store.index(target_corpus, target_vectors)

    # tens of thousands of near-perfect matches to the query, same
    # provider/model, but NEVER passed through this instance's index() —
    # ineligible stale rows, not a different patient/model.
    conn = _raw_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rag_embeddings "
            "(record_id, patient_id, provider, model, dimension, content_hash, embedding) "
            "SELECT 'unscoped-noise-' || gs, 1043, %s, '', %s, 'noise-' || gs, "
            "       ('[1' || repeat(',0', %s - 1) || ']')::vector "
            "FROM generate_series(1, 20000) gs",
            (_PROVIDER, _DIMENSION, _DIMENSION),
        )
    conn.commit()
    conn.close()

    result = pg_store.retrieve_top_k(query_vector, k=5)  # unscoped

    assert {record.record_id for record in result} == {record.record_id for record in target_corpus}


def test_pgvector_scoped_retrieval_returns_all_eligible_rows_at_scale():
    # A large-scale correctness demonstration alongside the mechanism test
    # above: even with tens of thousands of numerically closer rows
    # belonging to another patient in the SAME active corpus, a scoped query
    # returns every eligible row for the target patient. The noise rows are
    # added to pg_store._corpus_by_id (not just the DB) via a direct bulk
    # INSERT + dict update — bypassing 20,000 individual index() round trips
    # while still faithfully simulating a realistic multi-patient corpus
    # where every patient's records share one _corpus_by_id (that's how
    # run_pipeline actually populates it — see libs/rag_corpus/corpus.py).
    target_patient_id = 1042
    noise_patient_id = 1043
    query_vector = [1.0] + [0.0] * (_DIMENSION - 1)

    pg_store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    target_corpus = [
        CorpusRecord(
            record_id=f"recall-target-{i:04d}",
            patient_id=target_patient_id,
            patient_name="x",
            text=f"target record {i}",
            occurred_at="2026-01-01",
        )
        for i in range(5)
    ]
    # far from the query (orthogonal direction) — the only rows for this patient
    target_vectors = {
        record.record_id: [0.0] * (_DIMENSION - 1) + [1.0 + i * 0.01] for i, record in enumerate(target_corpus)
    }
    pg_store.index(target_corpus, target_vectors)

    # tens of thousands of near-perfect matches to the query, all belonging
    # to a DIFFERENT patient in the SAME corpus — the adversarial background
    # that biases the HNSW graph search away from the target cluster.
    noise_ids = [f"recall-noise-{i}" for i in range(1, 20001)]
    conn = _raw_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rag_embeddings "
            "(record_id, patient_id, provider, model, dimension, content_hash, embedding) "
            "SELECT 'recall-noise-' || gs, %s, %s, '', %s, 'noise-' || gs, "
            "       ('[1' || repeat(',0', %s - 1) || ']')::vector "
            "FROM generate_series(1, 20000) gs",
            (noise_patient_id, _PROVIDER, _DIMENSION, _DIMENSION),
        )
    conn.commit()
    conn.close()
    pg_store._corpus_by_id.update(
        {
            record_id: CorpusRecord(
                record_id=record_id, patient_id=noise_patient_id, patient_name="x", text="noise", occurred_at="x"
            )
            for record_id in noise_ids
        }
    )

    result = pg_store.retrieve_top_k(query_vector, k=5, patient_id=target_patient_id)

    assert {record.record_id for record in result} == {record.record_id for record in target_corpus}


def test_pgvector_stale_row_not_in_the_active_corpus_never_consumes_a_limit_slot():
    # Regression test for the follow-up review finding: rag_embeddings never
    # deletes rows on its own, so a row left behind by an older run (or a
    # since-lowered RAG_CORPUS_MAX_RECORDS) can still be ranked by a naive
    # query. This proves retrieve_top_k excludes it rather than letting it
    # occupy a LIMIT slot and then silently vanish.
    pg_store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    current = CorpusRecord(record_id="current-r1", patient_id=1042, patient_name="x", text="t", occurred_at="x")
    pg_store.index([current], {"current-r1": [0.9, 0.1] + [0.0] * (_DIMENSION - 2)})

    # a leftover row from a previous run, never indexed by THIS instance —
    # a perfect match to the query, so a naive query would rank it first.
    query_vector = [1.0] + [0.0] * (_DIMENSION - 1)
    conn = _raw_connection()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rag_embeddings "
            "(record_id, patient_id, provider, model, dimension, content_hash, embedding) "
            "VALUES ('stale-r1', 1042, %s, '', %s, 'stale-hash', %s)",
            (_PROVIDER, _DIMENSION, Vector(query_vector)),
        )
    conn.commit()
    conn.close()

    result = pg_store.retrieve_top_k(query_vector, k=1)

    assert [record.record_id for record in result] == ["current-r1"]


def test_pgvector_a_scoped_read_followed_by_an_unscoped_read_both_succeed_on_the_same_store():
    # Since the exact-scan fix now applies unconditionally (see above), a
    # scoped read no longer leaves the connection in any state DIFFERENT from
    # an unscoped one — there's nothing asymmetric left to leak. This just
    # confirms consecutive calls of different scope on the same store/
    # connection don't interfere with each other (rollback-per-call, proven
    # at the unit level, keeps the connection usable across both).
    pg_store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    r1 = CorpusRecord(record_id="r1", patient_id=1042, patient_name="x", text="t", occurred_at="2026-01-01")
    query_vector = [1.0] + [0.0] * (_DIMENSION - 1)
    pg_store.index([r1], {"r1": query_vector})

    scoped = pg_store.retrieve_top_k(query_vector, k=1, patient_id=1042)
    unscoped = pg_store.retrieve_top_k(query_vector, k=1)

    assert [r.record_id for r in scoped] == ["r1"]
    assert [r.record_id for r in unscoped] == ["r1"]


def test_pgvector_model_swap_under_the_same_provider_does_not_leak_across_models():
    # Regression test for the PR #14 review finding: the old conflict key
    # (record_id, provider) let a model swap under the same provider (e.g. a
    # different OLLAMA_EMBED_MODEL) silently reuse a stale vector. This
    # proves the fix end-to-end: two models' rows persist independently and
    # a query against one model never returns the other model's record, even
    # when the other model's vector would have ranked closer.
    patient_id = 1042
    query_vector = [1.0] + [0.0] * (_DIMENSION - 1)

    store_a = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider="ollama", model="model-a")
    store_b = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider="ollama", model="model-b")

    record_a = CorpusRecord(record_id="rec-a", patient_id=patient_id, patient_name="x", text="a", occurred_at="2026-01-01")
    record_b = CorpusRecord(record_id="rec-b", patient_id=patient_id, patient_name="x", text="b", occurred_at="2026-01-01")

    # rec_a (model-a) is a decent-but-imperfect match to the query; rec_b
    # (model-b) is a strictly BETTER numeric match — isolation means
    # store_a must still return rec_a, never rec_b.
    store_a.index([record_a], {"rec-a": [0.9, 0.1] + [0.0] * (_DIMENSION - 2)})
    store_b.index([record_b], {"rec-b": query_vector})  # a perfect match

    result = store_a.retrieve_top_k(query_vector, k=1)

    assert [record.record_id for record in result] == ["rec-a"]


def test_pgvector_reindexing_with_a_smaller_corpus_evicts_the_dropped_record_for_real():
    # Regression test for the fifth review finding: rag_embeddings
    # intentionally keeps r2's row after a re-index that drops it (migration
    # 010's "no cleanup" design) — the ONLY thing keeping it unreachable is
    # PgVectorStore.index() replacing (not merging into) _corpus_by_id, so
    # the record_id = ANY(:eligible_ids) constraint on the NEXT retrieve_top_k
    # call no longer includes it. Proven here against a real query (not just
    # the constraint that gets built) that the stale row is truly excluded,
    # including on a patient-scoped read for r2's own patient_id.
    store = PgVectorStore(config=_DB_CONFIG, dimension=_DIMENSION, provider=_PROVIDER)
    r1 = CorpusRecord(record_id="r1", patient_id=1042, patient_name="x", text="r1", occurred_at="2026-01-01")
    r2 = CorpusRecord(record_id="r2", patient_id=1043, patient_name="x", text="r2", occurred_at="2026-01-01")
    query_vector = [1.0] + [0.0] * (_DIMENSION - 1)
    store.index([r1, r2], {"r1": query_vector, "r2": query_vector})  # r2 is an equally perfect match

    store.index([r1], {"r1": query_vector})  # re-index without r2

    with store._conn.cursor() as cur:
        cur.execute("SELECT record_id FROM rag_embeddings WHERE provider = %s AND record_id = 'r2'", (_PROVIDER,))
        assert cur.fetchone() is not None, "r2's row must still physically exist (migration 010's no-cleanup design)"

    unscoped = store.retrieve_top_k(query_vector, k=5)
    assert "r2" not in {record.record_id for record in unscoped}

    scoped = store.retrieve_top_k(query_vector, k=5, patient_id=1043)  # r2's own patient_id
    assert scoped == []
