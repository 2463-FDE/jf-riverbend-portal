# Riverbend Synthetic RAG Corpus

This directory is the curated ingestion source for the Riverbend training project. Every document is synthetic. It contains no real patient data and is not a production policy library.

## Authority boundary

`manifest.json` is the authority for approval, audience, workflow, source identity, version, and file location. Document prose is untrusted retrieval content and must never widen its own scope.

Before reading document text, an ingestion or retrieval implementation must:

1. Reject an absolute path, path traversal, symlink escape, non-Markdown file, oversized file, missing file, duplicate source/version, or unsupported encoding.
2. Require `retrieval_enabled=true`, `approval_status=approved_training`, and `synthetic=true`.
3. Apply caller-derived audience and workflow filters. These are not model arguments.
4. Permit a model to narrow only the already-authorized topic set.
5. Preserve `source_id`, `source_version`, section identity, heading path, and workflow/audience metadata on every chunk and citation.

A policy document is evidence of the synthetic rule being explained. It is not evidence that a software feature exists, that an authorization is valid, or that a clinical, coverage, benefit, consent, or disclosure decision should be made.

## Chunking

The documents are longer than the current summary agent's 1,200-character retrieval budget. Do not truncate each document once at the beginning. Split deterministically by Markdown headings, then split only an oversized section into bounded overlapping chunks.

Recommended defaults are recorded in the manifest:

- maximum 1,200 characters per chunk;
- 120-character overlap only within the same section;
- stable key `source_id@source_version#section_id`;
- heading path retained as metadata;
- no chunk may combine different documents, audiences, workflows, or versions.

## Retrieval separation

Do not place every document into the patient-summary search scope.

- `patient_summary`: laboratory release, A1c education, threshold governance, medication-list safety, and urgent-warning education.
- `summary_review`: clinician review, source priority, and threshold governance.
- `scheduling`: appointment scheduling only.
- `records_access`: records-access policy only.
- `intake_consent`: intake and consent only.
- `roi`: ROI policy and records-access constraints; it cannot authorize release.
- `secure_messaging`: messaging, records-access, medication-safety, and urgent-warning constraints; care-team membership comes from active grants, not the document or model.
- `coverage_eligibility`: eligibility guidance only; it cannot determine benefits or payment.

Structured patient data must remain behind separately authorized application tools. Never add patient records, message bodies, intake forms, eligibility responses, or ROI payloads to this policy corpus.

## Vector and graph stores

The current training implementation stores deterministic heading-section chunks and Bedrock embeddings in PostgreSQL/pgvector. Authorization, approval, audience, and workflow filters are applied independently of similarity; vector similarity never replaces those checks.

New or changed documents require an explicit re-ingestion before they can appear in retrieval. The manifest and content hashes remain authoritative even after vectors are written.

The reproducible administrative sequence at this stage is:

1. run the focused manifest/corpus tests;
2. run `db/policy_corpus_ingest.py` with the configured policy embedding model.

A stacked follow-up PR adds `db/policy_corpus_evaluate.py`, which extends this
sequence with `--verify-only` (require manifest/database parity) and `--top-k 5`
(the sanitized client-case retrieval report). That evaluator reports evaluation
IDs and citation metadata, never questions, retrieved text, prompts, responses,
credentials, or raw provider errors; its `agent_refusal_accuracy` remains unset
because retrieval correctness alone cannot prove that generated prose refused or
escalated correctly.

The manifest also declares document relationships suitable for a future graph projection. A graph store may represent documents and declared edges, but must not infer new authorization, clinical authority, or workflow permissions from graph connectivity.

## Canonical files

The corpus contains one canonical file per `source_id@source_version`. Superseded fixtures may be declared in the manifest only with retrieval disabled. Earlier duplicate Secure Messaging candidates and the overlapping Coverage and Benefits draft are intentionally excluded. The coverage guide distinguishes durable statuses (`active`, `inactive`, `unknown`, `pending`, `stale`) from transient runtime/UI categories (`simulated`, `unavailable`).
