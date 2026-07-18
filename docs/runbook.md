# Riverbend Patient Portal — Operations Runbook

Practical "how do I run / fix this" notes for whoever is on call. Stack is Docker
Compose; one stack per clinic region.

## Start / stop

```bash
make up        # docker compose up -d (Postgres seeds on first boot via initdb)
make down      # stop the stack
make logs      # tail all logs
make ps        # service status (docker compose ps)
```

Endpoints once up:
- Portal: http://localhost:3070
- Gateway + OpenAPI docs: http://localhost:8070/docs
- Per-service health: `GET http://localhost:807N/healthz`

## First-boot data

On a fresh volume Postgres runs `db/schema.sql` then `db/seed/seed.sql`
automatically (mounted into `/docker-entrypoint-initdb.d`). To reload demo data
into an already-running DB:

```bash
make seed
```

To regenerate the seed file (deterministic):

```bash
python3 db/seed/generate_seed.py > db/seed/seed.sql
```

## Demo accounts

All seeded users share password `portal123`, role `staff`. Examples:
`frontdesk`, `rdelgado`, `drnguyen`, `roiclerk`, `mokonkwo`.
(Full list: `db/seed/generate_seed.py`.)

## Health checks

```bash
curl -s localhost:8070/healthz        # gateway
for p in 8071 8072 8073 8074 8075 8076; do curl -s localhost:$p/healthz; echo; done
```

A service that won't become healthy is almost always (a) Postgres not ready yet
or (b) bad DB creds in `.env`. Check `make logs`.

## Common incidents

### "Registration spins for 4–5 seconds" (RIV-088) — FIXED (Stage 3)
As of the Stage 3 async eligibility path (see
`adr/0005-eligibility-agent-runtime-and-resilience.md`), `/intake` no longer
verifies eligibility inline. Patient/coverage/consent persist first, then one
bounded, fast call (`ELIGIBILITY_JOB_ENQUEUE_TIMEOUT_SECONDS`, default 3s)
enqueues a job on `eligibility-service` and `/intake` returns `201`
immediately with `eligibility_status=pending` + `eligibility_job_id`. If you
still see multi-second `/intake` latency, check `elapsed_seconds` in the
response — it should be small; a large value means something is wrong with
Postgres commits, not eligibility.

### "Whole intake screen froze ~20 min" (RIV-141) — FIXED for /intake (Stage 3)
`/intake` can no longer be frozen by a payer outage — see above. A payer
outage now shows up as eligibility jobs cycling through `retryable` and
eventually `dead_letter` (see "Eligibility job queue" below), not a frozen
UI. The underlying payer call itself is still bounded by Stage 1's
timeout/retry/breaker regardless of caller (inline `/eligibility` or the
async job path both go through the same `check()`).

### "Two confirmations / two people for one slot" (RIV-175)
Double-booking from the check-then-insert race (no UNIQUE on `appointments.slot_id`,
no idempotency). To find duplicates:

```sql
SELECT slot_id, count(*) FROM appointments
WHERE status='confirmed' GROUP BY slot_id HAVING count(*) > 1;
```

Resolve manually (cancel the later row) until the booking path is fixed.

### "Allergy list differs between charts for the same patient" (RIV-160)
Duplicate-patient problem: self-service intake created multiple charts for one
person (no match key), and inbound HL7 AL1/RXA segments are dropped by the
parser. Reconcile charts manually; do not assume one chart is complete.

### DB connection errors after a restart
Postgres healthcheck gates the app services, but if you `down -v` you wipe the
volume and lose data; next `up` re-seeds from scratch.

## Eligibility job queue, breaker, cache, and dead-letter jobs (Stage 3)

See `adr/0005-eligibility-agent-runtime-and-resilience.md` for the full
design. Everything below lives in Redis, in `eligibility-service`
(`jobs.py` for the state machine, `worker.py` for the in-process consumer).

### Checking a job's status
```bash
curl -s localhost:8072/eligibility/jobs/<job_id> | python3 -m json.tool
# or, authenticated, through the gateway (what the portal itself uses):
curl -s localhost:8070/eligibility/jobs/<job_id> -H "Authorization: Bearer <token>"
```
States: `queued` -> `running` -> `succeeded` (a usable answer — active,
inactive, or stale) or `failed` -> `retryable` (bounded by
`ELIGIBILITY_JOB_MAX_RETRIES`, default 3) -> `dead_letter` once retries are
exhausted.

### A job is stuck in `dead_letter`
Check `error` on the job (an exception TYPE only — `RetriesExhaustedError`,
`CircuitOpenError`, etc. — never a raw payer message). This almost always
means the payer/clearinghouse is down or unreachable (see `PAYER_API_URL`),
the same underlying condition RIV-141 originally described. A front-desk
user (or `curl`) can request one controlled manual retry:
```bash
curl -s -X POST localhost:8070/eligibility/jobs/<job_id>/retry -H "Authorization: Bearer <token>"
```
Returns `409` (not the job, unchanged) once `manual_retry_count` has reached
`ELIGIBILITY_JOB_MAX_MANUAL_RETRIES` (default 1) — this is by design, not a
bug; it exists so "retry" can't be clicked forever against a payer that's
genuinely down.

### Checking the circuit breaker / last-known-good cache (Stage 1, still load-bearing)
The breaker is process-local (in-memory), so its live state isn't directly
queryable — infer it from job outcomes: a run of jobs failing instantly with
`error=CircuitOpenError` means the breaker is open (payer is being skipped
entirely until `ELIGIBILITY_BREAKER_RESET_SECONDS` elapses). A `stale`
`result_status` on a succeeded job means the live payer call failed but a
last-known-good cache entry (`elig:lkg:{insurance_id}` in Redis) was served
instead — check `result_checked_at` for how old that cached answer is.

### Worker-restart / "did I lose a job?"
No. The worker runs in-process inside `eligibility-service`, so a container
restart kills it, but every job's state lives in Redis, not the worker's
memory. On startup (and periodically), the worker reclaims any job left
`running` whose lease (`ELIGIBILITY_JOB_LEASE_SECONDS`, default 30s) has
expired — the previous worker died mid-check — and routes it back through
the same bounded retry-or-dead-letter path. To confirm nothing was dropped
after a restart:
```bash
docker compose restart eligibility-service
# wait a few seconds, then re-check any job that was in flight:
curl -s localhost:8072/eligibility/jobs/<job_id>
```
It should still exist and eventually reach a terminal state
(`succeeded`/`dead_letter`), never silently disappear.

### Switching the eligibility-assistant runtime
`ELIGIBILITY_AGENT_RUNTIME` (`.env`) selects `raw_bedrock` (default, no
framework) or `langchain` (comparison spike) for the
`POST /visits/{visit_id}/messages` chat endpoint. An unset or unrecognized
value fails closed (the service logs a `ValueError` and the endpoint
degrades to a safe "assistant unavailable" reply) rather than silently
picking a default. **No live Bedrock credential exists in this repo**
(`BEDROCK_MODEL_ID=changeme`) — expect every chat turn to return
`termination_reason=provider_error` with a generic "check manually" reply
until a real model id/region/credential is configured (see ADR 0005,
"Unresolved").

### PHI-safe diagnostics
When debugging any of the above, only ever log/paste the job's `error` field
(an exception TYPE name) and `status`/timestamps — never `insurance_id`, a
patient name, or a raw exception message. The same rule applies to the
metadata-only OpenTelemetry spans this stage adds (`libs/tracing`): spans
carry correlation ids, statuses, and counts only, never a prompt, model
reply, member id, or payload. If you need a wire-level payload for a payer
issue, capture it directly from the clearinghouse's own log, not from this
stack.

## Backups (current state)

There is **no automated backup/restore job** configured. For ad-hoc:

```bash
docker compose exec -T postgres pg_dump -U riverbend_app riverbend > backup.sql
```

This is a known gap (HIPAA contingency / data-backup plan) — flagged for the
next team.

## Logs & PHI warning

`logs/intake-service.log` currently contains full request bodies **including
PHI** (name/DOB/SSN). Treat the logs directory as sensitive; do not copy it off
the host. Removing PHI from logs is an open remediation item.

## CI

`.github/workflows/ci.yml`: frontend build, per-service import smoke, unit tests
(`pytest -m "not integration"`), then `docker compose build`. There is no
secret-scan, dependency-vuln-scan, or image-scan step — another known gap.
