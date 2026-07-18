# ADR 0005 — Async eligibility, resilient payer path, and a switchable eligibility agent

- **Status:** Accepted (Stage 3 of 3 — implemented)
- **Date:** 2026-07-18
- **Author:** Week 3 AI-readiness deliverable (this three-stage effort). Like
  ADR 0004, this is not authored by Helix Digital Partners (the original
  contractor) — no internal Riverbend team name exists in this repo to
  attribute it to (see `CLAUDE.md`, "Unknowns").

## Context

- `RIV-088` / `RIV-141` (`docs/handover/jira-tickets.md`; `ARCHITECTURE.md`
  §7; `docs/runbook.md`): `/intake` verified payer eligibility **inline**,
  synchronously, with no timeout. A slow payer made registration "spin
  ~4-5s"; a degraded/down payer froze the whole intake screen for the
  duration of the outage (observed ~20 min).
- The client separately asked for a small front-desk chat assistant that can
  check a patient's eligibility and hold basic visit context — built with
  **no framework** as the default (auditable, dependency-light) but
  **LangChain-swappable** behind one internal contract, so the two can be
  compared on the same test suite before committing to either long-term.
- This ADR covers all three stages, since Stage 3 is what actually wires the
  earlier two into a running, demoable system:
  - **Stage 1** (`135e453`, hardened by `106218a`): a bounded async payer
    client (timeout + jittered retry), a circuit breaker, and a Redis last-
    known-good cache, all inside `eligibility-service`. Fixed the
    transport-failure-mapped-to-"inactive" bug and made the cache best-effort.
  - **Stage 2** (`75f4f83`, hardened by `46b9007`): `libs/eligibility_agent`
    — a provider-neutral `AgentRuntime` contract with two implementations
    (`raw_bedrock` default, `langchain` comparison spike), one allow-listed
    `check_eligibility` tool, and structured (non-chat) visit memory. Neither
    runtime was wired into a running service yet.
  - **Stage 3** (this ADR): a Redis-backed async job queue for the payer
    check, `/intake` decoupled from payer latency, authenticated gateway
    routes for job status/retry and visit-chat, the agent runtime and visit
    memory wired into `eligibility-service`, a minimal frontend status
    surface, metadata-only tracing, and the Docker/libs packaging fix needed
    to actually run any of it in a container.

## Decision

### 1. Sync-to-async: a Redis-backed job queue, not a message broker

`/intake` now persists patient, coverage, and consent rows first —
unconditionally, independent of payer latency — then makes ONE bounded,
fast HTTP call to `eligibility-service`'s new `POST /eligibility/jobs`
endpoint, which only enqueues a job (a Redis write) and returns
immediately. `/intake` responds `201` with `eligibility_status=pending` and
an opaque `eligibility_job_id`, while keeping the pre-existing
`IntakeResponse.eligibility` dict field (now populated with a
pending/degraded summary) for backward compatibility.

The queue itself is a plain Redis list (`elig:job:queue`) drained by an
in-process asyncio worker inside `eligibility-service`
(`services/eligibility-service/worker.py`) — **not** a new broker
(RabbitMQ/Kafka/SQS). This stack is documented as one instance per clinic
region (`ARCHITECTURE.md`; `breaker.py`'s circuit-breaker state makes the
same single-instance assumption already), so an in-process task is the same
scale mechanism the rest of Stage 1 already uses, not a new architectural
layer.

Job records (`services/eligibility-service/jobs.py`) live under their own
namespaced Redis keys, distinct from every other Redis use in this stack
(gateway sessions: `session:{token}`; Stage 1's last-known-good cache:
`elig:lkg:{insurance_id}`; Stage 2's visit memory: `agent:visit:{visit_id}`):

- `elig:job:record:{job_id}` — one job's state, minimized payload (job id,
  idempotency key, insurance id, lifecycle fields, and a terminal result
  **summary** — status + checked-at + exception TYPE only, never a raw
  payer payload or PHI beyond the insurance id already handled elsewhere).
- `elig:job:queue` — job ids waiting to be (re)claimed.
- `elig:job:idem:{key}` — idempotency key -> job id, so a retried enqueue
  (a network blip on intake-service's side, not a new registration) returns
  the SAME job instead of triggering a second live payer call.
- `elig:job:inflight` — job ids currently `RUNNING`, used only for
  worker-restart recovery.

Job ids are `uuid4().hex` — safe, opaque, non-guessable, mirroring gateway
session tokens (`services/gateway/security.py::create_session`).

### 2. Explicit states, bounded retries, status TTL

`QUEUED -> RUNNING -> SUCCEEDED` (a usable answer: active/inactive/stale) or
`RUNNING -> FAILED` (the live check came back `unknown`, or the worker
itself hit an unexpected exception) `-> RETRYABLE` (bounded by
`ELIGIBILITY_JOB_MAX_RETRIES`, default 3) `-> DEAD_LETTER` once exhausted.
`DEAD_LETTER` can be moved back to `RETRYABLE` exactly once by default (
`ELIGIBILITY_JOB_MAX_MANUAL_RETRIES=1`) via an explicit, authenticated
`POST /eligibility/jobs/{id}/retry` — a 409 is returned, not a silent no-op
or an unbounded retry, once that budget is used. Every job record carries
`ELIGIBILITY_JOB_STATUS_TTL_SECONDS` (default 3600s), so completed/dead-
lettered jobs are still pollable for a while but do not accumulate in Redis
forever.

### 3. Worker-restart safety without a broker's built-in redelivery

A `RUNNING` job's lease (`ELIGIBILITY_JOB_LEASE_SECONDS`, default 30s) is
checked on worker startup and periodically thereafter
(`reclaim_expired()`). A worker that dies mid-check (crash, container
restart) leaves its job's record and queue entry in Redis, not in the dead
process's memory; the next worker instance to start reclaims any
lease-expired `RUNNING` job through the exact same bounded
retry-or-dead-letter path a live failure uses. This was exercised directly
against a real `docker kill` of the `eligibility-service` container during
this stage's manual verification: the in-flight job survived, was reclaimed
by the restarted worker, and reached `DEAD_LETTER` on schedule — it was
never silently dropped.

### 4. Circuit breaker and stale cache (Stage 1, now load-bearing for the job path)

The worker calls `eligibility-service`'s own `check()` — the same Stage 1
resilient path (bounded retries + jittered backoff, circuit breaker,
last-known-good cache) the synchronous `/eligibility` endpoint already
uses, via the same module-level breaker/cache singletons. This means a
payer outage now degrades the SAME way whether a caller hits `/eligibility`
directly or goes through the async job path: fail-fast once the breaker
opens, serve a `stale` last-known-good result if one exists and is within
its stale window, otherwise `unknown` — never a fabricated `inactive`.

### 5. Runtime comparison: `raw_bedrock` stays the default

`ELIGIBILITY_AGENT_RUNTIME` still fails closed on an unset/unrecognized
value (`libs/eligibility_agent/runtime.py::build_agent_runtime`); `
raw_bedrock` (no framework, hand-written bounded tool loop) remains the
explicit default, `langchain` the comparison spike. Stage 3 wires whichever
runtime is selected into `eligibility-service`'s new
`POST /visits/{visit_id}/messages` endpoint
(`services/eligibility-service/agent_wiring.py`), reusing `RedisVisitMemory`
for structured (non-chat) visit context. Building the default runtime
validates `BEDROCK_MODEL_ID`/`AWS_REGION` at construction; that
construction failure is caught once, memoized, and degrades every
subsequent chat turn to a safe "assistant unavailable" reply rather than
retrying an identical failure on every message — this repo's own
`BEDROCK_MODEL_ID=changeme` means that is, in fact, the path this
environment always exercises (see "Unresolved" below).

### 6. Security posture: no new unauthenticated exposure

The three new gateway routes (`GET /eligibility/jobs/{id}`,
`POST /eligibility/jobs/{id}/retry`, `POST /visits/{visit_id}/messages`) sit
behind the exact same `Depends(require_session)` every existing gateway
route uses. No internal-service-to-service auth was added between the
gateway and `eligibility-service` — that is pre-existing, documented debt
(`ARCHITECTURE.md` §1; `CLAUDE.md`) this stage does not touch. These new
routes also inherit the SAME limitation as every other route: a valid
session is required, but it is never checked against the specific
`job_id`/`visit_id` being requested, because every account maps to the
single flat `staff` role (`config/roles.yaml`) — there is no per-action
authorization to scope it to (`RIV-201`). This is a deliberate decision to
document the existing gap rather than widen scope by inventing a bespoke
authorization model for just these two endpoints.

A safe, opaque correlation id (`uuid4().hex`, gateway; a request-supplied or
generated equivalent in intake-service/eligibility-service) is generated
per request and forwarded as `X-Request-Id`, used only for tying spans/logs
for one request together — never derived from a session, patient id, or
member id.

### 7. Metadata-only tracing

`libs/tracing` (new) wraps OpenTelemetry: spans/events carry only
correlation ids, statuses, counts, and durations. Every OTel import is
lazy, mirroring `libs/llm_client/providers/bedrock_provider.py`'s lazy
`boto3` import, so a service that hasn't installed/configured OTel gets a
transparent no-op instead of an `ImportError`, and an exporter/collector
outage degrades to a no-op rather than failing the request being
instrumented. Attribute dicts are additionally redacted defense-in-depth via
the same field-name backstop `libs.safe_logging` uses for structured log
data — never a substitute for the primary rule (never pass a prompt, model
response, request/tool body, member id, payer payload, or secret as an
attribute in the first place). OTel dependencies live only in the
requirements.txt of the two services that actually import `libs.tracing`
(`intake-service`, `eligibility-service`), never in root
`requirements-dev.txt`.

### 8. The Docker/libs import gap

Stage 3 is the first time any service imports `libs/` at runtime
(`libs.tracing` in both changed services; `libs.eligibility_agent` and its
transitive `libs.safe_logging`/`libs.llm_client` deps in
`eligibility-service`). Every service's Dockerfile previously built with
`context: ./services/<service>` and `COPY . .` — `libs/`, one level up, was
never in that build context and would have failed at container start with
`ModuleNotFoundError`. Fixed by changing ONLY `intake-service`'s and
`eligibility-service`'s `docker-compose.yml` build stanzas to
`context: .` (repo root) with an explicit `dockerfile:` path, and updating
those two Dockerfiles' `COPY` paths accordingly (`COPY
services/<service>/requirements.txt .`, `COPY services/<service>/ .`,
`COPY libs/ ./libs/`). A new root-level `.dockerignore` keeps that wider
build context lean. The other five services (`gateway`, `records-service`,
`scheduling-service`, `interop-service`, `roi-service`) are untouched —
still `context: ./services/<service>` with their own local
`.dockerignore` — since none of them import `libs/`.

Proved with `docker compose config -q`, `docker compose build` (all eight
service images, including the five untouched ones, to confirm no
regression), and a container-level `python -c "import app"` /
`import libs.tracing, libs.eligibility_agent, libs.safe_logging` check
against the built `intake-service`/`eligibility-service` images — captured
as an automated integration test
(`tests/integration/test_docker_import.py`), not just a one-time manual
check.

## Alternatives considered

- **A real message broker (RabbitMQ/Kafka/SQS) for the job queue.** Rejected
  as disproportionate to this stack's actual scale (one instance per
  clinic region) and explicitly out of scope per the approved plan ("do NOT
  build a durable message broker"). Redis is already a dependency of every
  service in this stack; reusing it avoids a new piece of infrastructure to
  operate, monitor, and secure.
- **A separate worker container/process.** Rejected for the same
  single-instance-per-region reason `breaker.py` already gives for keeping
  circuit-breaker state process-local: an in-process asyncio task started
  from `eligibility-service`'s own FastAPI startup event needs no new
  compose service, port, health check, or deployment unit, and a container
  restart naturally restarts the worker task along with the API it's
  colocated with.
- **Bolting tool-calling onto `libs/llm_client`'s existing `Provider`
  interface** (carried over from Stage 2, restated here since Stage 3 is
  what actually uses it): rejected to avoid distorting a general-purpose,
  completion-only client that every other caller (`libs/rag_eval`, anything
  using `LLMClient`) depends on staying simple. A separate tool-capable port
  (`libs/eligibility_agent/bedrock_tool_port.py`) exists instead.
- **Vendoring `libs/` into each consuming service** instead of changing the
  Docker build context. Rejected: it would create multiple physical copies
  of the same shared code to keep in sync by hand, the opposite of what
  `libs/` (a REAL shared package, unlike the deliberate per-service
  duplication `adr/0001` describes for `config.py`/`models.py`/etc.) is for.

## Consequences

- `/intake` latency for a registration WITH insurance is now bounded by one
  fast Redis-backed enqueue call (a few ms to low seconds under
  `ELIGIBILITY_JOB_ENQUEUE_TIMEOUT_SECONDS`, default 3s) instead of the old
  unbounded payer round-trip — closes `RIV-088`/`RIV-141` for the intake
  path specifically.
- Front-desk staff (and, soon, patients) see a `pending` status immediately
  and must poll or wait for the result, rather than an immediate final
  answer — a real UX change, mitigated by the frontend status surface
  (bounded polling, never shows `unknown`/`stale`/failed as
  `inactive`/current, and a manual retry action once available).
- Two more moving parts to operate: the Redis job queue and the in-process
  worker (see `docs/runbook.md`'s new "Eligibility job queue" section for
  diagnostics).
- `eligibility-service` now depends on OpenTelemetry (real install, not
  just `libs/tracing`'s optional/lazy contract) and, if a real
  `ELIGIBILITY_AGENT_RUNTIME=raw_bedrock` deployment is ever turned on,
  `boto3` — neither is in `requirements-dev.txt`, matching the existing
  Stage 1/2 convention of keeping CI's fake-only test run dependency-light.
- Does not fix the IDOR (`ARCHITECTURE.md` §7), non-expiring sessions, flat
  `staff` role (`RIV-201`), or gateway-trusts-services-blindly gaps — all
  pre-existing, documented debt this stage explicitly does not widen or
  attempt to close.

## Unresolved / production deployment

- **No live Bedrock credential exists anywhere in this repo**
  (`BEDROCK_MODEL_ID=changeme`, `LLM_PROVIDER=fake` in `.env`/`.env.example`)
  — the `raw_bedrock` runtime's real, end-to-end behavior (a genuine
  Converse tool-calling loop against a real model) has never been executed
  in this environment, by design (`docs/planning`/ADR conventions in this
  repo consistently treat "no real credential" as the expected state, not a
  gap to fill). Enabling it for real requires: a real `BEDROCK_MODEL_ID` +
  `AWS_REGION` + AWS credential chain entry, adding `boto3` to
  `eligibility-service/requirements.txt` (see
  `libs/eligibility_agent/requirements.txt`'s existing pin), and a live
  functional test this stage could not run.
- **`langchain` runtime is even less proven**: it has never been run
  against a real `langgraph`/`langchain_aws` install (only against a
  self-authored fake of LangGraph's documented API shape — see that
  module's own docstring). Enabling it in production requires installing
  `libs/eligibility_agent/requirements-langchain.txt` in whichever service
  hosts it and a Redis- or Postgres-backed `langgraph` checkpointer (never
  `InMemorySaver`).
- **How code actually reaches "production"** (a VM per clinic region, per
  `ARCHITECTURE.md`) remains unknown — this repo still has no CI/CD step
  that builds/pushes/deploys anywhere (`CLAUDE.md`, "Unknowns"). This stage
  adds new runtime configuration (`ELIGIBILITY_AGENT_RUNTIME`,
  `ELIGIBILITY_JOB_*`, `ELIGIBILITY_WORKER_*`) to `.env.example` only; how
  those values would actually be set in a real deployment is unresolved,
  same as every prior stage's config.
- **Multi-replica `eligibility-service` is not supported by this design**:
  the circuit breaker (Stage 1) and the job worker (Stage 3) are both
  process-local/single-instance assumptions, matching
  `ARCHITECTURE.md`'s documented one-instance-per-clinic-region topology.
  Running more than one replica would need a shared breaker store and
  would cause multiple workers to compete for the same Redis queue
  (harmless — `LPOP` is atomic and a job can only be claimed once — but
  wasteful, since idle workers would poll the same empty queue).

## Rollback

Each of the three stages is an independent, revertable commit
(`135e453`/`106218a`, `75f4f83`/`46b9007`, and this stage's commit). To roll
back Stage 3 specifically: revert its commit, restore the two Dockerfiles'
`context: ./services/<service>` short form and `COPY . .`, and drop the
`build.context`/`dockerfile` overrides from `docker-compose.yml` for
`intake-service`/`eligibility-service`. `/intake` reverts to the old
synchronous inline payer call (RIV-088/RIV-141 return); no data migration
is needed since no new persistent (Postgres) schema was added — the job
queue lives entirely in Redis and simply stops being written to once the
enqueue call is removed. The new gateway routes and frontend status
component are additive and can be left in place harmlessly if only the
backend enqueue behavior is rolled back, though they would then have
nothing to poll.

## Related

- `docs/handover/jira-tickets.md` — `RIV-088`, `RIV-141`.
- `ARCHITECTURE.md` §7 — documented debt this stage does and does not
  touch.
- `docs/runbook.md` — operational diagnostics for the job queue, breaker,
  cache, dead-letter jobs, and runtime switch (updated alongside this ADR).
- `docs/planning/phi-safe-logging-policy.md` — the logging rule
  `libs/tracing` mirrors for spans.
- Commits: `135e453`, `106218a` (Stage 1 + hardening), `75f4f83`, `46b9007`
  (Stage 2 + hardening).
