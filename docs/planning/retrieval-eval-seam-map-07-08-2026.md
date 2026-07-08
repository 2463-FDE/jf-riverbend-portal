# Retrieval-Eval Seam Map — 2026-07-08

- **Purpose:** Document where a retrieval-eval harness (Week 2's actual
  deliverable) would sit relative to the chart-view seam already documented in
  `docs/planning/onboarding-seam-map.md`, and what it would depend on. This is
  a planning document for Commit 1 of the Week 2 deliverable — it does not add
  a route, a service, or a dependency, and it does not build the retrieval
  helper itself.
- **Inputs:** `docs/analysis/system-audit-07-01-2026.md`,
  `docs/analysis/context-map-07-01-2026.md`,
  `docs/analysis/baseline-architecture-07-01-2026.md`,
  `docs/planning/ai-readiness-debt-log-07-04-2026.md`,
  `docs/planning/onboarding-seam-map.md`, `adr/0001-0003`.

## 1. Why this is an eval-harness seam, not a retrieval-helper seam

The client asked for a retrieval helper that surfaces relevant past records
the moment a clinician opens a chart, evaluated against the prior
contractor's "gold set" of question/record pairs. That gold set's provenance
is unverified — it was demoed, not validated (see
`docs/planning/gold-set-risk-log-07-08-2026.md`). Building the production
retrieval helper on top of an unverified gold set would mean shipping a
feature whose only quality signal is a number nobody has checked for meaning.
Week 2's actual scope is narrower: build the harness that tests whether the
gold set — and any retrieval approach measured against it — can be trusted at
all. The retrieval helper itself remains out of scope this week.

## 2. Where the seam is

This reuses the same attachment point `docs/planning/onboarding-seam-map.md`
already identified, with the eval harness sitting beside it rather than in
the live request path:

```
Browser -> Next.js frontend (:3070) -> Gateway BFF (:8070) -> [ six domain services ]
                                             ^
                                             |
                                   AI/retrieval seam (NOT IMPLEMENTED)
                                             |
                                             .  (offline, out-of-band)
                                             .
                                   Retrieval-eval harness (this deliverable)
                                   reads a capped corpus + cached embeddings,
                                   scores against the gold set, writes a report
```

The eval harness is **offline and out-of-band**: it does not run on the
gateway fan-out path, does not touch a live clinician session, and produces a
static report (recall@k, precision@k, duplicate-rate, fragment-coverage gap).
Nothing in this deliverable adds a runtime dependency to the chart-view
request path described in `docs/analysis/context-map-07-01-2026.md` §6 (Flow
B — chart access).

## 3. What the harness would depend on

| Dependency | Current state | Relevant finding |
|---|---|---|
| **Corpus** | No existing text corpus for records/encounters is packaged for retrieval; `db/seed` provides deterministic, non-PHI demo data suitable for a capped eval corpus. The client's raw patient/encounter export must not be used for anything that touches tests/CI or leaves the environment. | N/A — a build constraint, not an existing debt item |
| **Embeddings** | No embedding pipeline exists in this repo today. Week 1 delivered a provider-swappable LLM client (`libs/llm_client`) for completions; an embedding-capable provider path is separate work, not yet built. | Out of scope for Commit 1 (see Commit 2 in the implementation plan) |
| **Gold set** | Contractor-provided, demo-validated only ("looked great when he demoed it" is not evidence of recall/precision on a real corpus). Provenance and label quality are unverified. | See `docs/planning/gold-set-risk-log-07-08-2026.md` |
| **MPI / identity resolution** | None exists (`intake.yaml: match_key: none`; no unique match key on name/dob/ssn in `db/schema.sql`). One person's records can be split across multiple `patients` rows with no system-level indication. | `AUD-09` (`docs/analysis/system-audit-07-01-2026.md`) |

The MPI dependency is the one most likely to be underestimated: a retrieval
harness can report a clean recall@k/precision@k number while still never
retrieving a patient's other chart fragments, because those fragments live
under a different `patients.id` that the harness has no way to know is the
same person. See
`docs/planning/gold-set-risk-log-07-08-2026.md` for how this interacts with
the gold set specifically.

## 4. What this document is not

- Not a build spec — no route, service, embedding call, or eval script exists
  as a result of this document.
- Not a validation of the gold set — see the risk log for that assessment.
- Not a resolution of `AUD-09` — a match-key/MPI approach is proposed only via
  ADR later in this deliverable (Commit 3 of the implementation plan), scoped
  as a proposal, not schema or matching-logic changes.
- Not an edit to `docs/planning/onboarding-seam-map.md` — that document
  remains the reference for the general AI seam; this document is specific to
  the retrieval-eval harness and cross-references it rather than restating it.
