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

- `patient_summary`: laboratory release, A1c education, and threshold governance.
- `summary_review`: clinician review, source priority, and threshold governance.
- `scheduling`: appointment scheduling only.
- `records_access`: records-access policy only.
- `intake_consent`: intake and consent only.
- `roi`: ROI policy and records-access constraints; it cannot authorize release.
- `secure_messaging`: messaging and records-access constraints; care-team membership comes from active grants, not the document or model.
- `coverage_eligibility`: eligibility guidance only; it cannot determine benefits or payment.

Structured patient data must remain behind separately authorized application tools. Never add patient records, message bodies, intake forms, eligibility responses, or ROI payloads to this policy corpus.

## Vector and graph stores

Neither is needed for eleven documents. Deterministic workflow filtering plus heading-section retrieval is cheaper, easier to validate, and sufficient for the current demo.

If evaluation later demonstrates a retrieval-quality gap, a vector index may store section chunks only after the manifest filters are applied. Vector similarity must never replace approval, audience, workflow, patient authorization, or source-version checks.

The manifest also declares document relationships suitable for a future graph projection. A graph store may represent documents and declared edges, but must not infer new authorization, clinical authority, or workflow permissions from graph connectivity.

## Canonical files

The corpus contains one canonical file per `source_id@source_version`. Earlier duplicate Secure Messaging candidates and the overlapping Coverage and Benefits draft are intentionally excluded. The coverage guide distinguishes durable statuses (`active`, `inactive`, `unknown`, `pending`, `stale`) from transient runtime/UI categories (`simulated`, `unavailable`).
