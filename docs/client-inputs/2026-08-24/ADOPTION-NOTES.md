# Riverbend Client Input Adoption Notes

This directory preserves selected client-supplied evidence from the August 24, 2026 training package. Except for this file and `evaluations/citation-aliases.json` (locally derived — see **Derived artifacts** below), the copied files are byte-for-byte copies. They are not part of the active RAG corpus under `docs/RagDocs/`.

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
| Role-boundary policies (enumerated below) | `GUIDE-REC-ACCESS-001@1.1` |
| `POL-MED-RECON-SAFETY` | `POL-MED-RECON-SAFETY@2026-08-22` |
| `EDU-DER-EMERG` | `EDU-DER-EMERG@2026-08-22` |
| `POL-A1C-MONITOR-CURRENT` | `POL-A1C-MONITOR-CURRENT@2026-08-22` |
| `POL-A1C-MONITOR-STALE` | Maps to itself: manifest-declared, superseded, retrieval-disabled fixture (see **Known specification conflicts** below) |

### Role-boundary policy enumeration

`org_policy_workflow/` in the client package contains six distinct role-boundary documents. All six are merged into the single active `GUIDE-REC-ACCESS-001@1.1` guide rather than kept as six separate active sources:

| Client input | Covered in `GUIDE-REC-ACCESS-001@1.1` |
|---|---|
| `POL-FRONT-DESK-NO-CHART` | §4 "Explicit Role Boundaries" — front desk clinical-notes/lab-interpretation exclusion |
| `POL-LAB-WRITE-WITHOUT-READ` | §4 "Explicit Role Boundaries" — laboratory staff write-without-prior-read-access |
| `POL-BILLING-NO-NOTES` | §4 "Explicit Role Boundaries" — billing coverage/payment scope, notes and education excluded |
| `POL-IT-NO-PHI` | §4 "Explicit Role Boundaries" — IT administrator account/audit access, no patient-scoped retrieval |
| `POL-MANAGEMENT-OVERSIGHT` | §4 "Explicit Role Boundaries" — management oversight/reporting access, not chart convenience |
| `POL-NO-CROSS-PATIENT` | **Not represented in this guide's text.** The cross-patient boundary is enforced by `services/records-service/patient_access_gate.py` at the application layer, not by RAG policy content — there is no corresponding prose to cite. |

The external retrieval evaluations retain their original client citation IDs. An executable evaluation must use the mapping above for merged sources. Cases requiring one of the 80 deferred public snapshots or 17 citation-only records remain acceptance backlog rather than being weakened to pass against missing evidence.

## Known specification conflicts and deferred cases

The 28-case evaluation suite contains four cases whose pass criteria cannot be satisfied against the active corpus as adopted. These are documented here as spec conflicts/deferrals, not silently passed, weakened, or fixed by loosening the active manifest:

| Case(s) | Required citation | Disposition | Reason |
|---|---|---|---|
| `E09`, `E10`, `E25` | `POL-A1C-MONITOR-STALE@2023-01-15` | spec_conflict | The stale A1c memo is intentionally superseded and retrieval-disabled (`retrieval_enabled: false`, `approval_status: "superseded"`). Enabling it merely to satisfy these cases would violate the active manifest's own supersession rule. |
| `E19` | `CIT-ADA-SOC@2026-08-22` | deferred | Citation-only record with no source text to quote from; excluded from the active corpus rather than treated as factual prose. |
| `E03` | `TRN-014@2026-08-22` (mapped to `EDU-A1C-001@1.1`) | spec_conflict | The pass criteria requires quoting "7.5 percent in March 2026 and 6.2 percent in August 2026." The merged `EDU-A1C-001@1.1` synthetic example carries the 7.5%/6.2%/1.3-point values (satisfying sibling case `E04`'s arithmetic requirement) but does not carry the client's original month/year labels for those two values — the literal date-bound quote this case requires is not present in the active corpus's text. |

`E09`/`E10`/`E25` and `E19` classify as `spec_conflict`/`deferred` automatically, from the alias mapping's own inactive/excluded status — no override needed. `E03` requires an explicit `case_overrides` entry in `evaluations/citation-aliases.json`, since its merged target (`EDU-A1C-001@1.1`) is active and ingestable; the harness would otherwise score it as a source-level pass despite the missing date-specific evidence. In all four cases the evaluation harness reports the disposition by classification rather than scoring a pass or silently dropping the case.

## Deliberate exclusions

- No public-domain snapshot was copied into the active corpus in bulk.
- No citation-only record may support a factual claim or quotation.
- The prompt-injection document remains an unapproved test fixture and is not listed in the active manifest.
- The access-control matrix is retained as client evidence, not executable authorization. Application permissions and caller-derived retrieval scopes remain authoritative.
- Bedrock configuration is not RAG content and no credential value is retained here.
- The stale A1c memo is structurally valid but cannot pass the manifest's ingestion gate.

## Derived artifacts

`evaluations/citation-aliases.json` is authored by Riverbend engineering during this adoption, not copied from the client package — it does not appear in `PACKAGE-INVENTORY.txt` or `SHA256SUMS.txt`, both of which describe only the client-supplied package as received. Its own checksum is recorded here instead:

```
sha256: aea4be298e97f795fe993e538d8b7de44da6e50e5f9a720054603b1620c3c92f  evaluations/citation-aliases.json
```

Recompute and update this hash whenever the file changes; it is the provenance record for the one file in this directory that is not a byte-for-byte client copy.

## Re-ingestion requirement

Changes under `docs/RagDocs/` do not update existing vectors automatically. Re-run the approved policy-corpus ingestion command before using the added or revised sources in a demo, then verify that the active document versions and embedding counts match the current manifest.
