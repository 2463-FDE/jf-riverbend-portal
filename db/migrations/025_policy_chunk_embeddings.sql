-- 025_policy_chunk_embeddings — embeddings for the policy RAG corpus
-- (w-9-2-planner P2, embeddings/retrieval slice), additive on top of
-- migration 024's policy_documents/policy_chunks.
--
-- A SEPARATE table from policy_chunks itself, mirroring rag_embeddings
-- (migration 010)'s own separation of record identity from its embedding:
-- reingesting a chunk (a text/hash change) and re-embedding it are
-- independent operations, and a future embedding-model swap can add new
-- rows here without touching policy_chunks at all — old rows are kept, not
-- deleted, exactly like rag_embeddings keeps a retired model's vectors
-- (they simply stop being selected once retrieval filters on the current
-- provider/model pair).
--
-- Fixed at VECTOR(1024) for Amazon Titan Embed Text v2
-- (amazon.titan-embed-text-v2:0), the model libs/policy_corpus's Bedrock
-- provider is scoped to by default. A different model/dimension requires a
-- new additive migration to widen/replace this column, same as
-- rag_embeddings's own documented VECTOR(16)-is-fixed-to-one-provider
-- constraint.
--
-- No document/chunk TEXT lives here — only the vector and the identifiers
-- needed to re-associate it with policy_chunks and enforce the
-- provider/model/dimension isolation retrieval depends on.

CREATE TABLE IF NOT EXISTS policy_chunk_embeddings (
    id           SERIAL PRIMARY KEY,
    chunk_id     TEXT NOT NULL REFERENCES policy_chunks(chunk_id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,             -- e.g. "bedrock" | "fake" (fake for tests only)
    model        TEXT NOT NULL,             -- e.g. amazon.titan-embed-text-v2:0
    dimension    INTEGER NOT NULL,
    content_hash TEXT NOT NULL,             -- sha256 of the embedded chunk text; drives re-embed skip
    embedding    VECTOR(1024) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, provider, model)
);

-- No HNSW/ANN index: this corpus is small enough (eleven documents) that an
-- exact scan is both cheap and, per vector-rag.md, the deliberately
-- preferred choice over ANN complexity at this scale.
CREATE INDEX IF NOT EXISTS policy_chunk_embeddings_chunk_id_idx ON policy_chunk_embeddings (chunk_id);
