"""Metadata-filtered retrieval over the policy corpus (w-9-2-planner P2,
embeddings/retrieval slice) — the provider-neutral interface
vector-rag.md's "Retrieval interface and graph seam" describes. No graph
store and no policy-navigator agent wiring here; those are later slices —
this is the retriever + citation-ledger PRIMITIVE they will use.

Exact, filtered pgvector search — no ANN/HNSW. `conn` is
dependency-injectable (mirrors libs/rag_corpus/vector_store.py::
PgVectorStore) so tests never need a real Postgres or pgvector package.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class RetrievalScope:
    """Caller-derived, never model-derived: `audiences`/`workflows` come
    from the authenticated caller's own role/context, fixed BEFORE
    retrieve() ever runs. `topic`, if a caller passes one through from the
    model, may only NARROW this already-authorized set — it can never
    substitute for or widen audiences/workflows."""

    audiences: Tuple[str, ...]
    workflows: Tuple[str, ...]
    topic: Optional[str] = None


@dataclass(frozen=True)
class RetrievedChunk:
    citation_id: str  # source_id@source_version#section_id
    source_id: str
    source_version: str
    title: str
    effective_date: str
    section_id: str
    heading_path: Tuple[str, ...]
    score: float
    text: str  # ephemeral — returned to the caller only, never persisted/logged downstream


class RetrievalLedger:
    """Records exactly which chunks one retrieve() call actually returned,
    so a later "the model cited X" step can reject any citation never
    actually retrieved for THIS request (vector-rag.md: "Reject a model
    citation that was not present in that request's retrieval ledger") and
    resolve a valid one back to its source/version/title for display.
    Scoped to one call/turn — start a fresh ledger per request, never reuse
    one across turns."""

    def __init__(self):
        self._chunks: Dict[str, RetrievedChunk] = {}

    def record(self, chunks: List[RetrievedChunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.citation_id] = chunk

    def is_valid_citation(self, citation_id: str) -> bool:
        return citation_id in self._chunks

    def get(self, citation_id: str) -> Optional[RetrievedChunk]:
        return self._chunks.get(citation_id)

    @property
    def citation_ids(self) -> Tuple[str, ...]:
        return tuple(self._chunks)


class PolicyRetriever:
    def __init__(self, conn, embedding_client, *, provider: str, model: str, vector_cast=None):
        self._conn = conn
        self._embedding_client = embedding_client
        self._provider = provider
        self._model = model
        self._vector_cast = vector_cast

    def retrieve(self, query: str, scope: RetrievalScope, limit: int) -> List[RetrievedChunk]:
        # An unscoped caller is never authorized for anything — this also
        # avoids spending a real embedding call on a query that Postgres's
        # own `&&` semantics would return zero rows for anyway.
        if not scope.audiences or not scope.workflows:
            return []

        [query_vector] = self._embedding_client.embed([query])
        query_vector_param = self._vector_cast(query_vector) if self._vector_cast else query_vector

        # WHERE applies every approval/synthetic/audience/workflow/topic/
        # provider/model filter BEFORE the ORDER BY ranks anything —
        # similarity never widens the candidate set past what these filters
        # already authorized (vector-rag.md).
        sql = """
            SELECT d.source_id, d.source_version, d.title, d.effective_date,
                   c.section_id, c.heading_path, c.text, (e.embedding <=> %s) AS distance
            FROM policy_chunk_embeddings e
            JOIN policy_chunks c ON c.chunk_id = e.chunk_id
            JOIN policy_documents d ON d.id = c.document_id
            WHERE e.provider = %s AND e.model = %s
              AND d.synthetic AND d.retrieval_enabled AND d.approval_status = 'approved_training'
              AND d.audiences && %s::text[] AND d.workflows && %s::text[]
        """
        params: List = [
            query_vector_param, self._provider, self._model, list(scope.audiences), list(scope.workflows)
        ]
        if scope.topic is not None:
            sql += " AND %s = ANY(d.topics)"
            params.append(scope.topic)
        sql += " ORDER BY distance ASC LIMIT %s"
        params.append(limit)

        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        finally:
            self._conn.rollback()  # a pure read leaves nothing to commit

        return [
            RetrievedChunk(
                citation_id=f"{source_id}@{source_version}#{section_id}",
                source_id=source_id,
                source_version=source_version,
                title=title,
                effective_date=str(effective_date),
                section_id=section_id,
                heading_path=tuple(heading_path),
                score=1.0 - float(distance),
                text=text,
            )
            for (source_id, source_version, title, effective_date, section_id, heading_path, text, distance) in rows
        ]
