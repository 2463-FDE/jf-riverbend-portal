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
- Freshness integrity (adversarial-review finding, REQUIRED): when persisting a
  tool result to visit memory, record the eligibility-service's own
  verification time (the tool payload's `as_of`), NEVER `now()`. A stale
  last-known-good fallback carries its original, older `checked_at`; stamping
  `now()` records a stale result as freshly verified and corrupts audit/
  downstream trust. Parse `as_of` via a shared helper
  (`libs/eligibility_agent/contracts.py::parse_as_of`); on a failed/no-check
  path (absent/unparseable `as_of`), preserve the prior `eligibility_checked_at`
  rather than inventing a timestamp. `updated_at` may still be `now()`.
- Provider-error containment (adversarial-review finding, REQUIRED): the
  `AgentRuntime` contract is that no provider/tool failure escapes
  `handle_message`. Non-retryable Bedrock `ClientError`s (AccessDenied,
  ValidationException) and unexpected Converse response shapes must be
  normalized at the tool-capable port into the llm_client provider-error
  vocabulary (`ProviderCallError`), and `handle_message` must catch the
  provider-error base (`LLMClientError`) — not just the two retryable types —
  and return a safe `PROVIDER_ERROR` turn, logging the error TYPE only. For the
  LangChain runtime, whose third-party model raises library-specific exceptions
  that cannot be enumerated without the dep installed, catch broadly around
  ONLY the single model-invoke call and degrade the same way.
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

### Stage 3 Hardening Fix — make the Redis job queue crash-safe (REQUIRED)

**Why this is a hardening fix, not scope creep.** Same rationale as the Stage 1
Hardening Fix: this is a correction to a defect in code Stage 3 already shipped,
surfaced by adversarial review (Codex) after the commit — not new feature
scope. Stage 3's own headline promise (ADR 0005 §3: "a container restart never
silently loses a job") is contradicted by the shipped implementation, so this
restores an invariant the stage already claims rather than adding a new one.
Folding it into unrelated work would mix a resilience bugfix with a feature in
one commit, breaking the plan's per-commit-reviewable discipline. The repo's
own `feat(...)` → `fix(...)` history (e.g. `a4bafe6`/`e4ef08a`) is the
precedent.

Two HIGH-severity findings, both a genuine "accepted registration silently
loses its payer check" path (the exact RIV-088/RIV-141 failure the async queue
exists to prevent):

- Finding 1 — claim is not crash-safe (`services/eligibility-service/jobs.py`,
  `dequeue`/`_mark_running`). The old claim did `LPOP` from the queue, then a
  *separate* `SADD` into an `inflight` SET, then wrote `RUNNING`. A crash in the
  window after the pop but before the job is durably tracked leaves the job in
  **neither** the queue nor `inflight`, with its record still `QUEUED` —
  invisible to `reclaim_expired()`, which only scans `inflight` for
  lease-expired `RUNNING` jobs. Permanently dropped.
- Finding 2 — retry transition strands jobs and can kill the worker
  (`jobs.py::mark_failed_or_retry`; `worker.py::process_one`/`run_worker_loop`).
  The old transition wrote `RETRYABLE`, then `SREM`'d `inflight`, then `RPUSH`'d
  the queue as three separate ops: a crash between the last two strands the job
  in neither structure. The `RPUSH` was also uncaught by `process_one`/
  `run_worker_loop`, so one Redis blip during a retry terminates the background
  worker and stops ALL future eligibility jobs — violating both methods'
  "never raises" docstrings.

**Required design (atomic claim + atomic transitions + worker resilience):**

- Replace the `inflight` SET (`elig:job:inflight`) with a **processing LIST**
  (`elig:job:processing`) — a *recoverable* structure the reclaim path scans.
- Claim atomically: `dequeue` uses a single `LMOVE elig:job:queue
  elig:job:processing LEFT RIGHT` (redis-py 5.x `lmove`; FIFO preserved) so the
  job is never in limbo — the instant it leaves the queue it is in
  `processing`. Then set `RUNNING` + lease on the record. A crash before the
  `RUNNING`/lease write leaves a `QUEUED`-record entry sitting in `processing`,
  which reclaim treats as an orphan (no live lease) and re-drives.
- Make every lifecycle transition atomic with a Redis transaction pipeline
  (`redis.pipeline(transaction=True)` → MULTI/EXEC): `mark_succeeded` =
  set(record) + `LREM` processing; `mark_failed_or_retry` (retryable) =
  set(record) + `LREM` processing + `RPUSH` queue; (dead-letter) = set(record) +
  `LREM` processing; `retry_manually` = set(record) + `RPUSH` queue. MULTI/EXEC
  removes the observable "committed one op, crashed before the next" state: the
  transition either fully happened or did not happen at all (record still
  `RUNNING` in `processing` → reclaimed). This may double-deliver in a
  narrow window (at-least-once, standard for a reliable Redis queue) but never
  loses a job; the payer `check()` is a read and safe to repeat.
- `reclaim_expired()` scans the `processing` LIST (`LRANGE 0 -1`): an entry is
  "owned by a live worker" ONLY if its record is `RUNNING` with a lease in the
  future — everything else (missing record, `QUEUED`/no-lease orphan,
  lease-expired `RUNNING`) is re-driven through the same bounded
  `mark_failed_or_retry` path or `LREM`'d if terminal. This is sound under the
  single-instance-per-region assumption already documented (ARCHITECTURE.md;
  breaker.py) — reclaim and claim never run concurrently.
- Worker resilience: wrap the post-dequeue store transitions in `process_one`
  (and defensively the `process_one`/`reclaim_expired` calls in
  `run_worker_loop`) so a Redis write failure logs the error TYPE only and
  returns — the job stays in `processing`, its lease expires, and reclaim
  re-drives it — instead of propagating and terminating the loop.
- Tests: inject a failure between EACH step (after `LMOVE` before `RUNNING`;
  after `RUNNING` before terminal write; after record write before `LREM`;
  during the retry `RPUSH`) and assert the job is still recoverable (reclaim
  requeues or dead-letters it, never drops it). Assert the worker loop survives
  a store-write exception and keeps processing later jobs. Extend the in-memory
  Redis test double to cover `lmove`/`lrange`/`lrem`/`pipeline` (transaction),
  mirroring the existing `_FakeRedis` in `tests/test_eligibility_jobs.py`.
- Definition of Done: `pytest -m "not integration" -q` passes including the new
  crash-injection cases; no reachable interleaving of a crash/Redis error can
  leave an accepted registration's job unrecoverable; a store-write error can no
  longer terminate the worker loop. Update ADR 0005 §3 to describe the
  `processing`-list + atomic-transition mechanism (it currently describes the
  `inflight` set).
- Suggested commit: `fix(eligibility): make async job queue crash-safe with atomic claim and transitions`
- Follows the same per-run workflow as any stage (re-inspect, implement, test,
  self-review, completion report, STOP for manual commit).

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
2. Confirm which unit is next. Stages 1–3 and the Stage 1 Hardening Fix are all
   committed; the **Stage 3 Hardening Fix** (crash-safe job queue) is the
   current unit, unless the user explicitly says otherwise.
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
