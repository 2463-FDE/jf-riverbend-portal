# Riverbend Client Input Adoption Notes

This directory preserves selected client-supplied evidence from the August 24, 2026 training package. Except for this file, the copied files are byte-for-byte copies. They are not part of the active RAG corpus under `docs/RagDocs/`.

The original package contained 115 Markdown documents. All 115 manifest entries matched a file, all declared content hashes matched, and the package-level `SHA256SUMS.txt` check passed before adoption. The checksum file describes the complete external package, including files deliberately not copied here.

## Active-corpus mappings

| Client input | Active Riverbend source |
|---|---|
| `POL-001` | `LAB-REL-001@1.2` |
| `POL-007` | `LAB-REL-EXCEPTION-001@1.0` (clinician-only companion appendix) |
| `TRN-014`, `EDU-DER-A1C-LIMITS` | `EDU-A1C-001@1.1` |
| `POL-REVIEW-QUEUE` | `SOP-AI-REVIEW-001@1.1` |
| `POL-INSUFFICIENT-EVIDENCE`, conflict-resolution rule | `CLIN-SRC-PRIORITY-001@1.1` |
| `POL-SCHEDULER-SLOTS` | `SCHED-001@1.1` |
| `POL-ROI-DOCUMENT-LIST` | `ROI-DISC-001@1.1` |
| Role-boundary policies | `GUIDE-REC-ACCESS-001@1.1` |
| `POL-MED-RECON-SAFETY` | `POL-MED-RECON-SAFETY@2026-08-22` |
| `EDU-DER-EMERG` | `EDU-DER-EMERG@2026-08-22` |
| `POL-A1C-MONITOR-CURRENT` | `POL-A1C-MONITOR-CURRENT@2026-08-22` |
| `POL-A1C-MONITOR-STALE` | Manifest-declared, superseded, retrieval-disabled fixture |

The external retrieval evaluations retain their original client citation IDs. An executable evaluation must use the mapping above for merged sources. Cases requiring one of the 80 deferred public snapshots or 17 citation-only records remain acceptance backlog rather than being weakened to pass against missing evidence.

## Deliberate exclusions

- No public-domain snapshot was copied into the active corpus in bulk.
- No citation-only record may support a factual claim or quotation.
- The prompt-injection document remains an unapproved test fixture and is not listed in the active manifest.
- The access-control matrix is retained as client evidence, not executable authorization. Application permissions and caller-derived retrieval scopes remain authoritative.
- Bedrock configuration is not RAG content and no credential value is retained here.
- The stale A1c memo is structurally valid but cannot pass the manifest's ingestion gate.

## Re-ingestion requirement

Changes under `docs/RagDocs/` do not update existing vectors automatically. Re-run the approved policy-corpus ingestion command before using the added or revised sources in a demo, then verify that the active document versions and embedding counts match the current manifest.
