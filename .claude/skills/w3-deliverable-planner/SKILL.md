---
name: w3-deliverable-planner
description: Use this skill to implement the approved Week 3 deliverable — a resilient async eligibility path plus a switchable single-agent eligibility assistant (raw-Bedrock default + LangChain comparison spike, one check_eligibility tool, visit memory) — from the approved three-stage plan. Implements ONE stage at a time, adds and runs tests, self-reviews adversarially, writes a Word completion report, then STOPS for a manual commit. Never commits, pushes, or opens a PR.
---

# Week 3 Deliverable Planner / Implementer

## Purpose

Implement the approved Week 3 plan for Riverbend's eligibility intake assistant
with minimal supervision, one Git-commit-sized stage at a time, stopping for a
human commit between every stage.

The approved plan lives in
`W3_Analysis_and_Implementation_Plan.docx` (and the one-page summary
`W3_Pre-Implementation_Summary.docx`) in the Week 3 deliverables folder. If the
Word file is unavailable, use the three-stage plan reproduced under "Approved
Three-Stage Scope" below.

Client ask (context, not scope creep): a small front-desk chat assistant that
checks a patient's eligibility and keeps visit context, built on a
non-blocking, timeout-bounded, circuit-breaker-guarded eligibility call, plus
an ADR (sync -> async + graceful degradation). The research task requires the
agent to be built with **no framework AND LangChain-swappable** behind one
internal contract.

## Required Inputs (inspect before changing anything)

- `W3_Analysis_and_Implementation_Plan.docx` — the approved plan (source of truth).
- `services/intake-service/app.py`, `services/eligibility-service/{app.py,check.py,schemas.py,config.py}`,
  `services/gateway/app.py` — the current eligibility path.
- `libs/llm_client/` (client, `providers/base.py`, `providers/bedrock_provider.py`) — reuse; do NOT rebuild.
- `libs/safe_logging/` and `docs/planning/phi-safe-logging-policy.md` — logging rules.
- `db/schema.sql` (`insurance_coverages`), `docker-compose.yml`, `.env.example`,
  `.github/workflows/ci.yml`, `requirements-dev.txt`, and each affected
  service's `requirements.txt` + `Dockerfile`.
- `tests/test_bedrock_provider.py` — the canonical fake-provider test pattern to mirror.
- The current client request in the conversation. If it is missing or
  contradicts the approved plan, STOP and report the mismatch instead of
  guessing.

## Re-Inspection Gate (run every time before touching code)

1. Read the approved plan.
2. Re-inspect the repository (files above + `git status`, `git log --oneline -5`, `git diff --stat`).
3. Confirm the repo has not changed in a way that invalidates the plan
   (e.g. the eligibility path was already refactored, `libs/eligibility_agent`
   already exists, LangChain was already added). If it has, STOP and report
   before implementing.

## Approved Scope: Three Stages Plus One Gating Fix Commit

Implement in this order. **Exactly one commit-sized unit per run.** Each unit is
one manual commit. Preserve backward compatibility unless the plan says
otherwise.

Stage 1 is DONE and already committed (`135e453 feat(eligibility): add bounded
resilience and safe status handling`). A post-commit adversarial code review
(Codex) found two HIGH-severity regressions of Stage 1's own invariant in the
code as shipped. **The Stage 1 Hardening Fix below is a required prerequisite
and must be committed before any Stage 2 work begins.** This is a deviation
from the original "exactly three commits" instruction, justified below under
"Why this is a fourth commit, not scope creep."

### Stage 1 — Resilience foundation and shared contracts (DONE, committed)
- Feature: bounded async payer client (timeout, retry classification + jitter,
  injected clock/transport for tests), circuit-breaker state machine, Redis
  last-known-good cache (separate key prefix, fresh TTL + bounded stale window,
  never cache errors, store `checked_at` + stale age), and Pydantic contracts
  (`EligibilityStatus` = active|inactive|unknown|pending|stale, `EligibilityResult`,
  `VisitContext`, `AuditEvent`).
- Fixed the unknown-vs-inactive bug for *transport-level* failures (timeout,
  connection reset) in both `services/eligibility-service/app.py`/`check.py`
  and `services/intake-service/app.py`.
- DB migration `db/migrations/009_eligibility_status_values.sql` allows
  `pending`/`stale` status values.
- No new heavy dependency; reused `httpx` and the existing `redis` client.
- Committed: `feat(eligibility): add bounded resilience and safe status handling`

### Stage 1 Hardening Fix — close the HTTP-status and cache-availability gaps (REQUIRED, gates Stage 2)

**Why this is a fourth commit, not scope creep.** The original Week 3
instructions require exactly three stages/commits. This fix is not new
feature scope — it is a correction to a defect in code Stage 1 already
shipped, surfaced by adversarial review after the commit. This repo's own
history already uses this exact pattern (`a4bafe6 feat(rag): add retrieval
eval harness...` followed by `e4ef08a fix(rag): make fragment-gap metric
case-specific`), so a small `fix(...)` commit between a `feat(...)` commit and
the next stage is consistent with project convention. Folding it into Stage
2's commit would mix an agent-runtime feature with an unrelated resilience
bugfix in one commit, which breaks the plan's own commit-discipline rule
("each commit is independently reviewable and tested").

**Why it must happen before Stage 2, not after.** Stage 2 builds the
`check_eligibility` tool directly on top of `check.py`/`payer_client.py`/
`cache.py`. If the agent ships before this fix, the assistant will confidently
tell the front desk "inactive" during a payer rate-limit or outage, and a
Redis blip will surface as a hard failure instead of a graceful "unknown" —
exactly the two failure modes the whole eligibility-resilience effort exists
to prevent (RIV-088/RIV-141). Fixing it after Stage 2 would mean shipping and
then immediately re-touching the tool's error-handling assumptions.

- Fix A — HTTP-status misclassification
  (`services/eligibility-service/payer_client.py:63-68`): the `else` branch
  after a completed HTTP request currently treats ANY response (including
  429/500/502/503/504) as a successful, terminal answer via `active:
  resp.is_success`. Classify 429 and 5xx as a transient payer error (retry,
  same bounded/jittered path as timeouts), and only treat an explicit
  business-level response (e.g. a real 271/denial payload or another
  non-retryable status your clearinghouse defines) as a genuine
  active/inactive answer. After retries are exhausted on a transient status,
  raise the existing `RetriesExhaustedError` path so `check.py` returns
  `unknown`/`stale` and never caches it — do not invent a new status enum
  value for this.
- Fix B — cache is not best-effort
  (`services/eligibility-service/cache.py:51,54`, called from
  `check.py:87,98`): wrap the Redis calls in `LastKnownGoodCache.get`/`set` (or
  at their call sites in `check.py`, whichever keeps the failure-handling
  closest to the Redis boundary) in a catch for Redis/connection errors. Log
  the error TYPE only (no PHI, mirrors the existing pattern already used
  elsewhere in this file). On a `set` failure, swallow it and still return the
  live payer result to the caller. On a `get` failure during `_fallback`,
  treat it as a cache miss and return `unknown` — never let a Redis outage
  surface as an unhandled 500.
- Tests: add a payer-stub case returning 429/500/503 and assert the result is
  `unknown` (retried, then not cached as inactive); add a cache double that
  raises on `get`/`set` and assert `check()` still returns the live result
  (set-failure case) and still degrades to `unknown` (get-failure case)
  instead of raising.
- Definition of Done: `pytest -m "not integration" -q` passes including the
  new cases; a payer 503 never produces a cached `inactive`; a Redis outage
  never produces an unhandled exception from `/eligibility`.
- Suggested commit: `fix(eligibility): classify payer HTTP failures as transient and make cache best-effort`
- This unit follows the same per-run workflow as any stage below (re-inspect,
  implement, test, self-review, completion report, STOP for manual commit)
  before Stage 2 begins.

### Stage 2 — Switchable agent runtimes and visit memory
- Feature: `AgentRuntime` port + factory selected by `ELIGIBILITY_AGENT_RUNTIME`
  (fail closed on unknown); one allow-listed `check_eligibility` tool that calls
  Stage 1's client (the model cannot pass an endpoint, credential, or arbitrary
  patient ID); visit-memory port (structured fields, not raw chat; visit TTL).
- `raw_bedrock` runtime = DEFAULT: explicit Bedrock Converse tool loop with max
  turns, schema validation, safe tool errors, deterministic termination. Add a
  separate tool-capable port; do NOT distort the completion-only `Provider`
  interface in `libs/llm_client`.
- `langchain` runtime = comparison spike: minimal graph, Redis/Postgres
  checkpointer (never `InMemorySaver` beyond unit tests).
- LangChain is a NEW heavy dependency: put it in a dedicated optional manifest
  (e.g. `libs/eligibility_agent/requirements-langchain.txt`), NOT in
  `requirements-dev.txt`. Keep all provider/SDK imports lazy.
- Suggested commit: `feat(agent): add switchable raw-bedrock (default) and langchain eligibility runtimes`

### Stage 3 — Async intake, minimal UI, tracing, and ADR
- Feature: Redis-backed eligibility job (id, idempotency key, status TTL, retry
  count, dead-letter) + worker/consumer; `/intake` returns `201` promptly with
  `eligibility_status=pending` while KEEPING the existing
  `IntakeResponse.eligibility` field for backward compatibility; authenticated
  visit-chat endpoint on the gateway; a MINIMAL frontend status surface (fresh /
  pending / stale / unknown / retry — never show stale as current);
  metadata-only OTel export (payload capture off) correlated by
  request_id/visit_id/job_id/trace_id; `adr/0005-eligibility-agent-runtime-and-resilience.md`;
  runbook update.
- MUST FIX the Docker/libs gap: any service that imports `libs/eligibility_agent`
  will fail in its container because services build from `./services/<svc>` with
  `COPY . .` and `libs/` is not in that context. Either move the build context
  to the repo root and `COPY libs/`, or vendor the package into the consuming
  service. Prove it with `docker compose build` + a container import check.
- OTel deps go in the consuming service's requirements only.
- Suggested commit: `feat(intake): decouple eligibility with graceful degradation and document runtime choice`

## Hard Rules

- Implement ONLY the current stage. No unrelated refactoring. No scope
  expansion. Do NOT build a durable message broker, real MPI, or fix unrelated
  documented debt (IDOR, plaintext PHI, session expiry, flat role).
- Use existing project patterns before new abstractions. Reuse `libs/llm_client`,
  `libs/safe_logging`, `httpx`, `redis`, Pydantic v2.
- Never claim HIPAA compliance and never widen scope to fix all debt.
- Protect secrets/PHI. Never log or trace prompts, model responses, request
  bodies, tool payloads, member IDs, payer payloads, secrets, tokens, or
  third-party exception strings. Log the error TYPE only (mirror
  `libs/llm_client/client.py`). Follow `docs/planning/phi-safe-logging-policy.md`.
- Do NOT edit `.env`. Use placeholders in `.env.example` only (add
  `ELIGIBILITY_AGENT_RUNTIME`, resilience settings).
- Do NOT invent PHI-like sample data. Use fake/mocked providers and the existing
  deterministic seed data.
- Automated tests must use fakes/mocks — no live provider, no real PHI, no
  network. Inject fake `boto3`/LangChain into `sys.modules` and keep imports
  lazy so CI (which installs only `requirements-dev.txt` and runs the fake
  provider) stays green. Mirror `tests/test_bedrock_provider.py`.
- Add tests for the happy path AND important failure paths.
- Explain any deviation from the approved plan and why it was necessary.
- Stop and ask the user only when a critical decision cannot be made safely from
  available evidence (e.g. the visit-memory persistence/privacy question, the
  approved Bedrock model id/region).

## Git Restrictions

Read-only Git only: `git status`, `git diff`, `git diff --stat`, `git log`,
`git branch`, `git show`.

NEVER run: `git add`, `git commit`, `git push`, `git merge`, `git rebase`,
`gh pr create`, or anything that changes history or publishes. Show the user the
exact commands they can run manually; the user runs them.

## Per-Run Workflow (one stage)

1. Run the Re-Inspection Gate above.
2. Confirm which unit is next. Stage 1 is committed; the Stage 1 Hardening Fix
   is next and MUST land before Stage 2, unless the user explicitly says
   otherwise.
3. Implement only that unit per the approved plan.
4. Add or update tests (success + failure paths; fakes only).
5. Run `pytest -m "not integration" -q` (and, for Stage 3, the integration
   tests if the user has `make up` running). Capture the real output.
6. Perform an adversarial self-review: re-read the diff, look for PHI/secret
   leakage, broken backward compatibility, CI-import breakage, and container
   import breakage (Stage 3).
7. Produce the Word completion report (below).
8. STOP before committing. Show the Mandatory Stage Boundary message.
9. Wait for the user to confirm the commit is done (or give feedback) before the
   next stage.

## Required Completion Report After Every Stage

Write/update a Word (`.docx`) report (US Letter; title 15pt, headings 13pt, body
11pt) in the Week 3 deliverables folder, e.g.
`W3_Stage<N>_Completion_Report.docx`, containing:

- Stage number and title; feature implemented; problem resolved.
- Summary of modifications; remaining work; deviations from the approved plan
  (and why each was necessary).
- Modified-files table: **File | Change Made | Problem Addressed | Risk or Impact**.
- Automated test commands actually run, and their real results. Do NOT claim a
  test passed unless it ran and passed. List tests not executed and why.
- Manual testing steps and detailed demo steps: `make up`; `make seed`; portal
  `http://localhost:3070`; gateway `http://localhost:8070`.
- Demo credentials found in the repo: username `frontdesk` (or `rdelgado` /
  `jpark`), password `portal123` (all seeded accounts; documented in
  README/runbook); every account has the single flat role `staff`. State
  clearly that live Bedrock is NOT available in the repo
  (`BEDROCK_MODEL_ID=changeme`, `LLM_PROVIDER=fake`) so demos use the fake
  provider / fake model scripts.
- Visible UI behavior; and backend behavior not visible in the UI (which API
  request, which service, which store/provider, and which metadata-only log or
  test evidence confirms it).
- Suggested manual commit message and the Git commands to review + commit
  manually.

## Mandatory Stage Boundary

After each stage, stop and display a message like:

> Stage N code changes and tests are complete. I did not create a commit. Please
> review the completion report, inspect the changes, and commit manually. Tell
> me when the commit is complete or give feedback before I begin Stage N+1.

Do not begin the next stage until the user explicitly confirms. Apply the same
rule after Stages 1, 2, and 3.

## After Stage 3

Produce the final implementation summary and the proposed pull request
description (title, purpose, main changes, problems resolved, security
considerations, testing completed, demo steps, known limitations, follow-up).
Do NOT create the pull request.
