"""Swappable retrieval interface: the pure-Python cosine ranker and a
pgvector-backed ANN store sit behind the same VectorStore contract, selected
via a fail-closed factory that mirrors
libs/eligibility_agent/runtime.py::build_agent_runtime — same shape,
same reason (config-only selection, no silent fallback to an unrecognized
name).

PgVectorStore's psycopg2/pgvector imports are deferred into __init__ (and
skipped entirely when a `connection` is injected, as tests do) so importing
this module — and everything that depends on it, including the default
in-memory path — never requires psycopg2 or the pgvector package installed.
Only actually connecting to Postgres does.

Every pgvector query that accepts a `patient_id` filters
`WHERE patient_id = %s` — the retrieval-path analogue of the RIV-201
boundary (adr/0006 §2). This is defense in depth for retrieval specifically;
it does not remediate that unresolved gateway/records IDOR.

Two correctness properties this module holds itself to, per PR #14 review
(see .claude/skills/langgraph-imp-planner/SKILL.md, "Lessons learned"):

- A patient-scoped query must never under-return: pgvector's HNSW index
  applies `patient_id = %s` AFTER the approximate graph search, so a plain
  `ORDER BY embedding <=> %s LIMIT k` can silently return fewer than k rows
  even when k+ eligible rows exist for that patient (this is pgvector's own
  documented filtered-ANN limitation, not a bug in Postgres). Verified by
  hand against a live pgvector 0.8.0 database: enabling
  `hnsw.iterative_scan = strict_order` did NOT reliably fix this for an
  adversarially-clustered patient (it still returned 0 of 5 true matches in
  one reproduction — iterative scan's own graph-traversal termination can
  give up before reaching a poorly-connected cluster). Forcing an exact scan
  for scoped queries (disabling index/bitmap scan methods for that query)
  DID reliably return the correct, complete result in the same
  reproduction — see retrieve_top_k below. Exactness over ANN speed is the
  right trade for this specific path: it is the security-scoped query, and
  this corpus is demonstration-sized by design (adr/0006), so an O(n)
  filtered scan is cheap.
- A model change under the same provider must never mix embedding spaces:
  `provider` alone (e.g. "ollama") does not capture *which* model produced a
  vector (OLLAMA_EMBED_MODEL can change independently). `model` is part of
  the persisted identity (UNIQUE key) and the retrieval filter so a retired
  model's vectors are never compared against a new model's query vectors.
- A stale persisted row must never silently consume a LIMIT slot: rag_embeddings
  never deletes rows on its own (an older corpus run, a since-lowered
  RAG_CORPUS_MAX_RECORDS, a renamed/removed seed record), so ranking over
  every row for a provider/model and only mapping the survivors back through
  `_corpus_by_id` could return fewer than k results even when k+ CURRENT
  records exist — a stale row could occupy a top-k slot and then get dropped.
  retrieve_top_k constrains the SQL itself to `record_id = ANY(:eligible_ids)`
  (this instance's own indexed corpus) instead of filtering after LIMIT.
- A scoped read's `SET LOCAL` must never leak into a later read: it lasts
  until end of TRANSACTION, not end of cursor, and psycopg2 connections don't
  autocommit — so without an explicit rollback, an unscoped query issued
  after a scoped one on the same connection would inherit the scoped query's
  disabled indexes. retrieve_top_k rolls back (a plain read has nothing to
  commit) in a `finally` after every call.
- Re-indexing must REPLACE the active corpus, never merge into it: index()
  builds `_corpus_by_id` fresh from exactly the records it was just given
  (for PgVectorStore, swapped in only after a successful commit, so a failed
  index() call leaves the prior complete state untouched) instead of adding
  to whatever was there before. An append-only `_corpus_by_id` would keep a
  record removed from a later index() call — e.g. a shrunk
  RAG_CORPUS_MAX_RECORDS or a renamed/removed seed record — permanently
  "eligible" via the `record_id = ANY(:eligible_ids)` constraint above,
  including on patient-scoped reads if that stale record carried the
  requested patient_id. Both InMemoryCosineStore and PgVectorStore had this
  bug — it's in the shared contract, not one backend's quirk.
- A failed index() must roll back before propagating: without it, psycopg2
  leaves the transaction "aborted" after any failed statement, so every
  later call on that connection (index() again, or retrieve_top_k()) keeps
  failing until something else rolls it back — turning one transient DB
  error into a lasting outage for that store instance rather than something
  a caller can retry.
"""
import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

from libs.safe_logging import get_safe_logger

from .config import VectorStoreConfig
from .corpus import CorpusRecord

log = get_safe_logger(__name__)

_KNOWN_STORES = ("memory", "pgvector")


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    # Mirrors libs/rag_eval/similarity.py::cosine_similarity exactly.
    # Duplicated rather than imported to avoid a reverse dependency from this
    # package (libs/rag_corpus) onto libs/rag_eval, which already depends on
    # libs/rag_corpus.
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _content_hash(text: str) -> str:
    # Mirrors libs/rag_corpus/embedding_cache.py::_content_hash — same
    # rationale for duplication as _cosine_similarity above.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_tag_for_provider(provider: str) -> str:
    """The model/version half of the persisted embedding identity (the other
    half is `provider`) — folded into rag_embeddings so a model change under
    the same provider (e.g. a different OLLAMA_EMBED_MODEL) is never confused
    with an unchanged embedding space. Empty for "fake": FakeEmbeddingProvider
    has no model concept (see its own docstring)."""
    if provider == "ollama":
        return os.getenv("OLLAMA_EMBED_MODEL", "")
    return ""


@dataclass(frozen=True)
class IndexResult:
    written: int
    skipped: int


class VectorStore(ABC):
    @abstractmethod
    def index(self, corpus: List[CorpusRecord], vectors_by_record_id: Dict[str, List[float]]) -> IndexResult:
        """Make `corpus`/`vectors_by_record_id` available to retrieve_top_k.
        Must be safe to call repeatedly over the same records: a record whose
        embedding is unchanged (same content_hash) must be skipped, not
        rewritten."""
        raise NotImplementedError

    @abstractmethod
    def retrieve_top_k(
        self, query_vector: List[float], k: int, *, patient_id: Optional[int] = None
    ) -> List[CorpusRecord]:
        """Return up to k CorpusRecords ranked by similarity to query_vector.
        When patient_id is given, only that patient's records are eligible —
        a cross-patient query must return an empty list, never another
        patient's record."""
        raise NotImplementedError


class InMemoryCosineStore(VectorStore):
    """The no-infrastructure default and fallback: wraps the existing
    pure-Python cosine ranker. Nothing here is persisted anywhere — index()
    just keeps corpus/vectors in memory for the lifetime of this instance."""

    def __init__(self):
        self._corpus_by_id: Dict[str, CorpusRecord] = {}
        self._vectors_by_record_id: Dict[str, List[float]] = {}

    def index(self, corpus: List[CorpusRecord], vectors_by_record_id: Dict[str, List[float]]) -> IndexResult:
        # Replace, not merge: a record present in an earlier index() call but
        # absent from THIS corpus must become unreachable — an append-only
        # _corpus_by_id would keep a removed record eligible forever,
        # including on patient-scoped reads if it carried the requested
        # patient_id.
        self._corpus_by_id = {record.record_id: record for record in corpus}
        self._vectors_by_record_id = {record.record_id: vectors_by_record_id[record.record_id] for record in corpus}
        log.info("in_memory_cosine_store indexed corpus (total=%s)", len(corpus))
        return IndexResult(written=len(corpus), skipped=0)

    def retrieve_top_k(
        self, query_vector: List[float], k: int, *, patient_id: Optional[int] = None
    ) -> List[CorpusRecord]:
        candidates = [
            record
            for record in self._corpus_by_id.values()
            if patient_id is None or record.patient_id == patient_id
        ]
        scored = [
            (_cosine_similarity(query_vector, self._vectors_by_record_id[record.record_id]), record)
            for record in candidates
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [record for _, record in scored[:k]]


class PgVectorStore(VectorStore):
    """ANN retrieval over the `rag_embeddings` table
    (db/migrations/010_pgvector_embeddings.sql). The patient_id filter and
    the ANN search are one query under the existing Postgres ACLs.

    `connection` is dependency-injectable so tests can drive this class
    against a fake psycopg2-shaped double with no real Postgres or pgvector
    package present — see tests/test_rag_vector_store.py. In production
    (connection=None), a real connection is opened lazily here, and only here.
    """

    def __init__(
        self,
        config: Optional[VectorStoreConfig] = None,
        dimension: int = 16,
        provider: str = "fake",
        model: Optional[str] = None,
        connection=None,
    ):
        self._dimension = dimension
        self._provider = provider
        # Auto-derived from `provider` when not given explicitly, so a caller
        # that only passes `provider="ollama"` still gets the active
        # OLLAMA_EMBED_MODEL folded into the persisted identity rather than
        # silently defaulting to "no model tracking".
        self._model = model if model is not None else model_tag_for_provider(provider)
        self._corpus_by_id: Dict[str, CorpusRecord] = {}
        # Real psycopg2 needs each vector parameter wrapped as pgvector.Vector
        # (register_vector's adapter otherwise only fires for that wrapper —
        # a plain Python list is sent as a numeric[] and the vector <=>
        # operator doesn't exist for that type). The fake connection used by
        # tests/test_rag_vector_store.py has no such requirement, so this
        # stays None there and vectors pass through as plain lists.
        self._vector_cast = None

        if connection is not None:
            self._conn = connection
        else:
            import psycopg2
            from pgvector import Vector
            from pgvector.psycopg2 import register_vector

            cfg = config or VectorStoreConfig()
            self._conn = psycopg2.connect(
                host=cfg.db_host,
                port=cfg.db_port,
                dbname=cfg.db_name,
                user=cfg.db_user,
                password=cfg.db_password,
            )
            register_vector(self._conn)
            self._vector_cast = Vector

    def _check_dimension(self, record_id: str, vector: List[float]) -> None:
        if len(vector) != self._dimension:
            raise ValueError(
                f"embedding dimension mismatch for {record_id}: "
                f"expected {self._dimension}, got {len(vector)}"
            )

    def index(self, corpus: List[CorpusRecord], vectors_by_record_id: Dict[str, List[float]]) -> IndexResult:
        written = 0
        skipped = 0
        # Built separately from self._corpus_by_id and only swapped in after
        # a successful commit below: replace, not merge. rag_embeddings
        # intentionally keeps old rows across re-indexes (see migration 010),
        # so retrieve_top_k's record_id = ANY(eligible_ids) constraint is the
        # ONLY thing keeping a since-removed record unreachable — an
        # append-only _corpus_by_id would defeat that on the very next call,
        # including on patient-scoped reads if the stale record carried the
        # requested patient_id. A failed commit leaves self._corpus_by_id
        # exactly as it was before this call, never partially updated.
        new_corpus_by_id: Dict[str, CorpusRecord] = {}
        try:
            with self._conn.cursor() as cur:
                for record in corpus:
                    new_corpus_by_id[record.record_id] = record
                    vector = vectors_by_record_id[record.record_id]
                    self._check_dimension(record.record_id, vector)
                    vector_param = self._vector_cast(vector) if self._vector_cast else vector
                    cur.execute(
                        """
                        INSERT INTO rag_embeddings
                            (record_id, patient_id, provider, model, dimension, content_hash, embedding, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (record_id, provider, model) DO UPDATE
                            SET patient_id = EXCLUDED.patient_id,
                                dimension = EXCLUDED.dimension,
                                content_hash = EXCLUDED.content_hash,
                                embedding = EXCLUDED.embedding,
                                updated_at = now()
                            WHERE rag_embeddings.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                               OR rag_embeddings.dimension IS DISTINCT FROM EXCLUDED.dimension
                               OR rag_embeddings.patient_id IS DISTINCT FROM EXCLUDED.patient_id
                        """,
                        (
                            record.record_id,
                            record.patient_id,
                            self._provider,
                            self._model,
                            self._dimension,
                            _content_hash(record.text),
                            vector_param,
                        ),
                    )
                    if cur.rowcount:
                        written += 1
                    else:
                        skipped += 1
            self._conn.commit()
        except Exception:
            # Without this, a failed statement (a transient DB error, a
            # deadlock, mid-batch failure) leaves psycopg2's transaction
            # "aborted" — every later call on this same connection (index()
            # again, or retrieve_top_k()) would keep failing until something
            # else rolls it back, turning one transient blip into a lasting
            # outage for this store instance.
            self._conn.rollback()
            raise
        self._corpus_by_id = new_corpus_by_id
        log.info(
            "pgvector_store indexed corpus (provider=%s, total=%s, written=%s, skipped=%s)",
            self._provider,
            len(corpus),
            written,
            skipped,
        )
        return IndexResult(written=written, skipped=skipped)

    def retrieve_top_k(
        self, query_vector: List[float], k: int, *, patient_id: Optional[int] = None
    ) -> List[CorpusRecord]:
        self._check_dimension("<query>", query_vector)

        # Constrain the ranked set to records THIS instance has actually
        # indexed. rag_embeddings is persistent and never deletes rows on its
        # own (e.g. an older corpus run, a since-lowered RAG_CORPUS_MAX_RECORDS,
        # or a renamed/removed seed record can all leave rows behind) — without
        # this, a stale row can occupy a LIMIT slot and get silently dropped
        # below, so a caller asking for k results can quietly get fewer even
        # though k+ CURRENT records exist elsewhere in the ranked order.
        eligible_ids = list(self._corpus_by_id.keys())
        if not eligible_ids:
            return []

        sql = "SELECT record_id FROM rag_embeddings WHERE provider = %s AND model = %s AND record_id = ANY(%s)"
        params: List = [self._provider, self._model, eligible_ids]
        if patient_id is not None:
            sql += " AND patient_id = %s"
            params.append(patient_id)
        sql += " ORDER BY embedding <=> %s LIMIT %s"
        query_vector_param = self._vector_cast(query_vector) if self._vector_cast else query_vector
        params += [query_vector_param, k]

        try:
            with self._conn.cursor() as cur:
                if patient_id is not None:
                    # pgvector's HNSW index applies a WHERE filter AFTER the
                    # approximate graph search, so a selective patient_id
                    # predicate can silently return fewer than k rows even when
                    # k+ exist for that patient. Verified `hnsw.iterative_scan`
                    # does not reliably fix this (see this module's docstring);
                    # forcing an exact scan does. This is the patient-scope
                    # security boundary, so correctness wins over ANN speed —
                    # and this corpus is demonstration-sized (adr/0006), so the
                    # O(n) cost is cheap.
                    cur.execute("SET LOCAL enable_indexscan = off")
                    cur.execute("SET LOCAL enable_bitmapscan = off")
                    cur.execute("SET LOCAL enable_indexonlyscan = off")
                cur.execute(sql, params)
                record_ids = [row[0] for row in cur.fetchall()]
        finally:
            # SET LOCAL lasts until end of transaction, not end of cursor —
            # without this, a scoped read's exact-scan settings would leak
            # into every later read on this same connection (including
            # unscoped ones), degrading them for no reason. Also avoids
            # leaving the connection idle-in-transaction after a pure read.
            self._conn.rollback()

        log.info(
            "pgvector_store retrieve_top_k ok (provider=%s, k=%s, patient_scoped=%s, returned=%s)",
            self._provider,
            k,
            patient_id is not None,
            len(record_ids),
        )
        # Every record_id here came from `ANY(eligible_ids)` above, so this
        # lookup can never miss — a KeyError would mean that invariant broke,
        # and should raise loudly rather than silently drop a result.
        return [self._corpus_by_id[record_id] for record_id in record_ids]


def build_vector_store(name: str = None, **kwargs) -> VectorStore:
    """Fail closed: an unset/unrecognized RAG_VECTOR_STORE raises rather than
    silently falling back to any default, mirroring
    libs/eligibility_agent/runtime.py::build_agent_runtime. `memory` must be
    requested explicitly (by name or via the env var) — it is the configured
    default, not an implicit fallback for an unrecognized value."""
    name = name or os.getenv("RAG_VECTOR_STORE", "memory")

    if name == "memory":
        return InMemoryCosineStore()

    if name == "pgvector":
        return PgVectorStore(**kwargs)

    raise ValueError(f"Unknown RAG_VECTOR_STORE '{name}' — expected one of: {', '.join(_KNOWN_STORES)}")
