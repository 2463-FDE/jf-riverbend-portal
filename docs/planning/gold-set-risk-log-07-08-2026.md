# Gold-Set Provenance / Trust Risk Log — 2026-07-08

- **Purpose:** Record, as a new and explicitly-labeled risk entry, that the
  prior contractor's retrieval "gold set" has not been validated and cannot
  be treated as ground truth until the Week 2 eval harness checks it. This is
  an addendum to `docs/planning/ai-readiness-debt-log-07-04-2026.md` — that
  file is not edited; it is a prior dated artifact per `CLAUDE.md`'s rule
  against editing prior dated reports.
- **Relationship to existing IDs:** None of the existing `AUD-*`
  (`docs/analysis/system-audit-07-01-2026.md`) or `DEBT D#` (in-code) findings
  describe this risk — it is not a security or correctness defect in the
  existing codebase, it is a data-quality/provenance gap in an artifact
  supplied for a new deliverable. Per the naming rule in
  `.claude/skills/w2-deliverable-planner/SKILL.md`, it is introduced under a
  new ID namespace (`RISK-GS-*`) rather than force-mapped onto an AUD/DEBT ID
  that doesn't fit.

## RISK-GS-01 — Gold-set provenance and label trust is unverified

- **Severity:** High (blocks trusting any recall@k/precision@k number
  produced against it, not a defect in the running system)
- **Status:** Open — this is the reason the Week 2 eval harness exists
- **Evidence:** The gold set was produced and demoed by the prior contractor
  as part of the client's retrieval-helper ask. No test corpus, labeling
  methodology, inter-rater agreement, or record-provenance documentation for
  it exists anywhere in this repository. "It looked good in a demo" is an
  anecdotal claim, not a validated measurement — there is no artifact in this
  repo that shows the gold set was checked against a real corpus with a
  defined metric before today.
- **Risk:** Any retrieval approach — including a well-implemented one —
  measured only against an unverified gold set can report misleadingly good
  numbers for reasons that have nothing to do with retrieval quality:
  mislabeled expected-relevant records, a gold set that only covers "easy"
  single-fragment patients, or expected answers that were hand-picked from
  the same records the retriever was tuned against. None of this is
  detectable from the recall@k/precision@k numbers alone.
- **Compounding factor — fragmented identity (`AUD-09`):** This is the
  specific reason recall/precision alone can look "fine" while a retrieval
  approach is still missing a patient's other chart fragments. Because there
  is no master-patient-index (`intake.yaml: match_key: none`; no unique match
  key on name/dob/ssn in `db/schema.sql`; real-world manifestation in RIV-160,
  where the same patient's allergy list differed by which chart was opened),
  a gold-set question written against one patient's chart row can score a
  perfect recall@k hit against that one fragment while the corpus silently
  contains a second, third, or further fragment for the same person that was
  never retrieved and never counted as a miss — because the gold set itself
  has no way to express "this patient has other fragments" if whoever built
  it worked from one chart row per person, the same assumption the system
  itself makes. A gold set built without correcting for `AUD-09` will
  systematically under-count exactly the failure mode `AUD-09` produces. This
  is why the eval harness's metric definitions include `duplicate-rate` and
  `fragment-coverage gap` alongside recall@k/precision@k, not as a
  replacement for them — a harness that reports only recall/precision would
  inherit this blind spot instead of surfacing it.
- **What would resolve this (not in scope for this deliverable):** Either an
  independently-audited gold set with documented labeling methodology, or an
  eval harness result set that reports fragment-coverage gap and
  duplicate-rate alongside recall/precision so the blind spot is visible
  rather than resolved. Actual identity resolution (a real MPI/match-key
  fix for `AUD-09`) is proposed only via ADR later in this deliverable and is
  not implemented here.
- **Do not do:** Do not treat a high recall@k/precision@k score from this
  harness as proof the gold set (or a retrieval approach measured against it)
  is trustworthy, until fragment-coverage gap and duplicate-rate are also
  reviewed. Do not build the production retrieval helper on top of this gold
  set until this entry is closed or explicitly risk-accepted by someone with
  the authority to accept that risk.

## Cross-references

- `docs/analysis/system-audit-07-01-2026.md` — `AUD-09` (no master-patient-index;
  fragmented identity), `AUD-06` (PHI logged in plaintext — relevant if
  corpus/query text is ever logged during eval runs, see
  `docs/planning/phi-safe-logging-policy.md`).
- `docs/planning/ai-readiness-debt-log-07-04-2026.md` — prior entry on
  `AUD-09` in the context of AI-feature readiness generally; this document
  narrows that same finding to its specific effect on retrieval-eval metrics.
- `docs/planning/onboarding-seam-map.md` and
  `docs/planning/retrieval-eval-seam-map-07-08-2026.md` — seam context for
  where a retrieval feature (and this eval harness) would attach.
- `adr/0001-monorepo-and-stack.md` — no shared library/schema convention
  across services, relevant to why no match-key was ever added at intake.

## Non-goals

This document does not validate or invalidate the gold set itself — that is
the eval harness's job (Commit 3 of the implementation plan). It does not
implement a fix for `AUD-09`. It does not modify
`docs/planning/ai-readiness-debt-log-07-04-2026.md`; it stands alongside it
as a dated addendum.
