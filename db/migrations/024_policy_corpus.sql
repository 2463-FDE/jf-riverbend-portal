-- 024_policy_corpus — additive schema for the synthetic policy RAG corpus
-- (w-9-2-planner P2, docs/RagDocs/manifest.json).
--
-- Deliberately NOT rag_embeddings (migration 010): that table models patient
-- encounter records (a mandatory patient_id FK) and is fixed at VECTOR(16)
-- for the fake embedding provider. Policy retrieval has no patient scope at
-- all — it is audience/workflow-scoped, and reads a single corpus fixed at
-- pipeline setup, not one row per (patient, record). Reusing rag_embeddings
-- would force a fake patient_id onto every policy chunk and lock the corpus
-- into a 16-dimension test-only vector space.
--
-- This migration is the corpus FOUNDATION only: manifest-derived document and
-- chunk metadata. It intentionally has no embedding column — that is an
-- additive follow-up once a real embedding provider/model is selected (see
-- vector-rag.md "Embeddings"), exactly how migration 010 itself was applied
-- on top of already-existing structures rather than speculatively upfront.
--
-- No patient facts, message bodies, prompts, or provider responses belong in
-- either table — only the manifest's own declared metadata and the
-- synthetic document text itself (see docs/RagDocs/manifest.json's
-- `patient_data_in_corpus: false`).

CREATE TABLE IF NOT EXISTS policy_documents (
    id                SERIAL PRIMARY KEY,
    corpus_id         TEXT NOT NULL,
    source_id         TEXT NOT NULL,
    source_version    TEXT NOT NULL,
    title             TEXT NOT NULL,
    owner             TEXT NOT NULL,
    effective_date    DATE,
    approval_status   TEXT NOT NULL,          -- e.g. approved_training — see manifest.ingestion contract
    synthetic         BOOLEAN NOT NULL,
    retrieval_enabled BOOLEAN NOT NULL,
    content_path      TEXT NOT NULL,          -- relative to docs/RagDocs/ — never absolute, never traversed
    content_sha256    TEXT NOT NULL,           -- must match the manifest's declared hash before ingest
    audiences         TEXT[] NOT NULL DEFAULT '{}',
    workflows         TEXT[] NOT NULL DEFAULT '{}',
    topics            TEXT[] NOT NULL DEFAULT '{}',
    allowed_uses      TEXT[] NOT NULL DEFAULT '{}',
    prohibited_uses   TEXT[] NOT NULL DEFAULT '{}',
    relationships     JSONB NOT NULL DEFAULT '[]',  -- [{"type": "governed_by", "target_source_id": "..."}]
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_version)
);

-- Retrieval will filter on audience/workflow membership before ranking
-- (vector-rag.md: "SQL applies ... audience, workflow ... filters before
-- ranking/limit"); GIN + array-overlap (&&) is the natural fit for that,
-- not yet exercised by this foundation slice but cheap to have in place.
CREATE INDEX IF NOT EXISTS policy_documents_audiences_gin_idx ON policy_documents USING gin (audiences);
CREATE INDEX IF NOT EXISTS policy_documents_workflows_gin_idx ON policy_documents USING gin (workflows);

CREATE TABLE IF NOT EXISTS policy_chunks (
    id             SERIAL PRIMARY KEY,
    chunk_id       TEXT NOT NULL UNIQUE,   -- stable: source_id@source_version#section_id
    document_id    INTEGER NOT NULL REFERENCES policy_documents(id) ON DELETE CASCADE,
    section_id     TEXT NOT NULL,
    heading_path   TEXT[] NOT NULL DEFAULT '{}',
    text           TEXT NOT NULL,
    chunk_hash     TEXT NOT NULL,           -- sha256 of `text` — drives idempotent-reingestion skip
    char_count     INTEGER NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS policy_chunks_document_id_idx ON policy_chunks (document_id);
