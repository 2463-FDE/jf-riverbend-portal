-- 010_pgvector_embeddings — persist the Week 2 RAG corpus's embeddings in
-- Postgres and add a pgvector ANN retrieval path behind the same interface
-- as the pure-Python cosine ranker (libs/rag_corpus/vector_store.py).
-- 2026-07-30 · Week 8 AI persistence & orchestration hardening (Stage 2 of 3,
-- see .claude/skills/langgraph-imp-planner/SKILL.md and adr/0006).
--
-- The vector row carries patient_id on the same row as its embedding so the
-- ANN search and the patient-scope predicate are ONE filtered query under the
-- existing Postgres ACLs (adr/0006 §2) — defense in depth for this retrieval
-- path specifically, NOT a fix for the unresolved RIV-201 gateway/records
-- IDOR. Additive and reversible: adding an extension, a new table, and two
-- new indexes touches no existing table or column.
--
-- embedding is fixed at vector(16), matching
-- libs/embedding_client/providers/fake_provider.py's FakeEmbeddingProvider
-- (_DIMENSIONS = 16) — the only embedding provider this repo's tests, CI, and
-- `make up` integration path actually exercise (there is no Ollama service in
-- docker-compose.yml, so EMBEDDING_PROVIDER=ollama is configured but never
-- run here). Switching to a provider with a different output dimension
-- requires a new additive migration to widen/replace this column — see the
-- pgvector revisit trigger in adr/0006.
--
-- `model` (added post-review, before this migration merged — see PR #14):
-- `provider` alone (fake | ollama) does not capture *which* model produced a
-- vector. OLLAMA_EMBED_MODEL can change while provider stays "ollama", which
-- would otherwise let a stale vector from a retired model silently survive
-- an unchanged content_hash and get compared against fresh query vectors
-- from the new model — a silent, wrong-similarity-space bug, not an error.
-- `model` is folded into the identity (UNIQUE key) and the retrieval filter
-- (libs/rag_corpus/vector_store.py) so different models never mix in one
-- ANN comparison; it is empty ('') for "fake" (FakeEmbeddingProvider has no
-- model concept).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_embeddings (
    id           SERIAL PRIMARY KEY,
    record_id    TEXT NOT NULL,             -- CorpusRecord.record_id, e.g. seed-enc-0001
    patient_id   INTEGER NOT NULL REFERENCES patients(id),
    provider     TEXT NOT NULL,             -- embedding provider tag (fake | ollama)
    model        TEXT NOT NULL DEFAULT '',  -- e.g. OLLAMA_EMBED_MODEL; '' for the fake provider
    dimension    INTEGER NOT NULL,
    content_hash TEXT NOT NULL,             -- sha256 of the embedded text; drives re-embed/re-write skip
    embedding    VECTOR(16) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (record_id, provider, model)
);
-- NOTE: no record text, patient name, or other PHI is stored here — only the
-- vector and the identifiers needed to re-associate it with the corpus and
-- authorize the read (libs/rag_corpus/vector_store.py keeps the corpus
-- itself in memory and looks record bodies up by record_id).
--
-- A model swap (different UNIQUE key) leaves the old model's rows in place
-- rather than deleting them — harmless, since retrieval always filters on
-- the currently-configured (provider, model) pair, so a retired model's
-- vectors are simply never selected. Cleanup of orphaned rows is future
-- work, not a correctness requirement.

CREATE INDEX IF NOT EXISTS rag_embeddings_patient_id_idx ON rag_embeddings (patient_id);

CREATE INDEX IF NOT EXISTS rag_embeddings_hnsw_idx
    ON rag_embeddings USING hnsw (embedding vector_cosine_ops);
