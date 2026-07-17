---
name: w2-deliverable-planner
description: Use this skill when planning Week 2's client deliverable (RAG retrieval eval harness + fragmentation report + MPI/match-key ADR) from system-audit, context-map, baseline-architecture docs, the AI-readiness debt log, and the client's retrieval-helper request. It should produce a safe implementation plan before modifying files.
---

# Week 2 Deliverable Planner

## Purpose

Plan a safe Week 2 implementation from the project documentation and the
client's retrieval-helper request.

Client message asked for a retrieval helper that surfaces relevant past
records the moment a clinician opens a chart, tested against the prior
contractor's "gold set." The actual Week 2 deliverable is narrower: an eval
harness that measures whether that gold set is trustworthy at all, plus the
identity-resolution ADR that Week 1's debt log already flagged as deferred.

## Required Inputs

Before changing files, inspect:

- `docs/analysis/system-audit-*` (esp. `AUD-09` fragmented-identity finding)
- `docs/analysis/context-map-*`
- `docs/analysis/baseline-architecture-*`
- `docs/planning/ai-readiness-debt-log-*` and `docs/planning/onboarding-seam-map.md`
- `libs/llm_client/` (provider-swappable client from Week 1 — reuse, don't rebuild)
- current client request if present in the conversation (retrieval helper /
  gold-set / cost note); if missing, ask the user for it before finalizing the
  plan
- relevant source files only after the plan is clear

If a required artifact is missing or contradicts this skill, stop and report
the mismatch instead of inventing replacement context.

## Rules

- You may read and inspect any files inside this project freely.
- You may search the repository, documentation, source code, tests, configs, and CI files as needed.
- Do not write, edit, delete, move, or format any files until I approve the plan.
- I will create branches and commits myself unless I explicitly ask you to do it.
- Do not run `git commit`, `git push`, or create branches.
- After planning, output the proposed PR title and PR description on screen.
- Do not build the full production retrieval helper ("surfaces relevant
  records the moment a chart opens") — the client's ask is broader than this
  week's deliverable, which is the eval harness + fragmentation report + ADR
  only.
- Do not treat the contractor's gold set as validated. Its provenance is
  unverified ("looked great when he demoed it" is not evidence) — the eval
  harness's job is to test that claim, not assume it.
- Do not send PHI or the client's raw patients/encounters export to any
  external embedding or completion API. Prefer a local/offline embedding
  provider (e.g. Ollama) over Anthropic/OpenAI for this deliverable so no
  patient data leaves the environment; if an external provider is used for
  eval-only synthetic queries, corpus text itself must stay local.
- Do not invent realistic PHI-like sample data for tests or fixtures. Use
  fake/mocked providers and non-PHI synthetic fixtures, or deterministic
  seed-derived fixtures where the repo already provides them. Do not fabricate
  realistic names, SSNs, DOBs, or clinical histories.
- Cap corpus size (config-driven limit). Embed once, cache to disk, and never
  re-embed on a repeat run — re-embedding on every eval run is the specific
  cost failure mode the client flagged.
- Mirror the Week 1 provider-swappable client patterns: timeout, retry/backoff,
  provider abstraction, token/cost guard, and safe logging. Reuse existing code
  where it fits; do not force embeddings through an incompatible
  completion-only interface.
- Do not hardcode, print, commit, or place API keys in prompts.
- Do not edit `.env` directly. Use placeholder values only in `.env.example`.
- Automated tests must use fake/mocked providers only — no real embedding or
  completion API calls, no real PHI.
- Do not silently "fix" `AUD-09` (build real MPI matching) this week — propose
  it via ADR only; implementation is a separate, larger effort.

## Metric Definitions

Use explicit metric definitions:

- `recall@k`: percentage of gold-set questions where at least one expected relevant record appears in the top-k retrieved records.
- `precision@k`: percentage of top-k retrieved records that are relevant according to the gold set.
- `duplicate-rate`: percentage of patient/person entities represented by more than one patient/chart fragment in the eval corpus.
- `fragment-coverage gap`: percentage of gold-set questions where the correct answer exists in the corpus but is attached to a different patient/chart fragment than the retrieved fragment.

If exact identity truth is unavailable, report the limitation clearly and mark the metric as estimated or proxy-based.


## Planning Output

Produce an implementation plan using exactly 4 commit-sized sections. Do not
create the commits yourself unless explicitly asked.

### Section 1: `docs: add retrieval-eval seam map and gold-set risk log`

Include:

- Short seam map: where a retrieval helper would plug into the existing chart
  view, and what it would depend on (corpus, embeddings, MPI).
- Debt-log addendum (append to or reference
  `docs/planning/ai-readiness-debt-log-*`, do not edit the prior dated file)
  naming the gold-set provenance risk explicitly — no existing AUD/DEBT ID
  covers "unverified vendor-demoed eval set," so introduce it as a new,
  clearly-labeled risk entry rather than mapping it onto an ID that doesn't
  fit.
- Cross-reference to `AUD-09` (fragmented identity) as the reason recall/
  precision numbers alone can look "fine" while still missing a patient's
  other fragments.
- No code changes.

### Section 2: `feat(rag): add capped corpus builder with cached local embeddings`

Include:

- Config-driven corpus size cap, built from `db/seed` deterministic data (not
  the client's raw export) for anything that touches tests/CI.
- Embed-once-and-cache pipeline (persisted embedding cache; a second run must
  not re-embed unchanged records).
- Embedding call routed through a provider-swappable interface consistent with
  the Week 1 client patterns, defaulting to a local/offline provider.
- Token/cost guard reused or adapted from the existing LLM client wrapper where
  the abstraction fits.
- No PHI, raw record text, or embeddings logged at INFO — reuse the Week 1
  PHI-safe logging/redaction helper.

### Section 3: `feat(rag): add retrieval eval harness, fragmentation report, and MPI ADR`

Include:

- Eval harness that runs the gold-set questions against the cached corpus and
  reports recall@k / precision@k.
- Fragmentation metrics surfaced in the same report: duplicate-rate and
  fragment-coverage gap (i.e., how often the gold answer lives in a different
  chart-row fragment than the one retrieved).
- New ADR (`adr/0004-*.md`) proposing an MPI/match-key approach to resolve
  `AUD-09`, scoped as a proposal only — no schema or matching-logic changes in
  this section.

### Section 4: `test: add RAG eval harness and embedding-cache tests`

Include tests for:

- Recall/precision calculation correctness on a small synthetic gold set.
- Duplicate-rate / fragment-coverage-gap metric calculation.
- Cache-hit path never re-embeds unchanged records.
- Redaction/no-PHI-logging on the corpus/embedding path.
- Fake/mocked embedding provider only — no real API calls.

Also inspect `.github/workflows/ci.yml`:

- If `pytest -m "not integration" -q` already picks up the new test file, do
  not modify CI unnecessarily.
- If a new dependency (e.g. a vector/cosine-similarity helper) is needed,
  add it to the relevant `requirements*.txt`, not just import it ad hoc.
- Do not add a CI step requiring a real embedding/completion API key.
- CI must validate the harness using the fake provider and synthetic fixtures
  only.

## Workflow

1. Inspect the repository and documentation freely.
2. Summarize relevant findings from the docs and code, especially `AUD-09`
   and the Week 1 debt log.
3. Explain why the full production retrieval helper is out of scope this week
   and why the gold set can't be trusted as-is.
4. Propose the exact 4-section plan.
5. Output a proposed PR title and PR description on screen.
6. Wait for approval before changing files.
7. After approval, implement one commit-sized section at a time.
8. Do not create branches, commit, or push. I will handle Git.
9. After each section, show:
   - files changed
   - tests run
   - remaining work

## Required Planning Response Format

When planning, respond with:

- Findings from repo inspection
- Scope boundary
- Risks and assumptions
- 4 commit-sized implementation sections
- Tests and CI impact
- Open questions
- Proposed PR title
- Proposed PR description
- Approval request before edits
