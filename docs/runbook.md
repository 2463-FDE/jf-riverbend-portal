# Riverbend Patient Portal — Operations Runbook

Practical "how do I run / fix this" notes for whoever is on call. Stack is Docker
Compose; one stack per clinic region.

## Required one-time setup: INTERNAL_SERVICE_TOKEN

Round-17 review (2026-08-06, PR #20): `gateway`, `intake-service`, and
`records-service` all now refuse to start (see each service's `lifespan`
handler in `app.py`) unless `INTERNAL_SERVICE_TOKEN` in `.env` is set to a
real random value at least 32 characters long — this is the shared secret
that proves an intake/patient-view call actually came through the gateway,
not a direct caller hitting a service's published host port. `.env.example`
ships this **empty on purpose** (a placeholder like `changeme` would be a
public, guessable secret every deployment shipped unmodified), so `.env`
needs it set explicitly before the first `make up`:

```bash
# generates a 64-char hex value; set the SAME value on all three services —
# they already share one .env, so setting it once here is enough
openssl rand -hex 32
```

Put that value in `.env`'s `INTERNAL_SERVICE_TOKEN=` line (see the detailed
comment above that line in `.env.example` for the full history). Without
it, the three services now fail fast at container startup with a clear
`RuntimeError` in `docker compose logs` (rather than starting and sitting
"unhealthy" until the healthcheck's retry budget runs out) — that log line
is the signal to come back here.

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

## Deploying a new release / rollback (Week 1 catch-up)

There is still no CI/CD pipeline that deploys to a clinic VM (see
`ARCHITECTURE.md` §1) — this section covers what to do manually once new code
reaches a VM, however that happens.

**Before restarting any service after a `git pull`:** apply any new database
migrations against the running Postgres *first*.

```bash
db/migrations/apply.sh
```

This runs every file in `db/migrations/` in order against the running
`postgres` compose service. It is safe to run on **any** existing database —
whether freshly seeded, stopped at an old migration, or already fully
migrated — because every migration in this directory uses `IF NOT EXISTS` /
guarded DDL: re-applying an already-applied migration is a no-op (you'll see
`NOTICE: ... already exists, skipping`), not an error. Run it after every
deploy, even if you're not sure whether the target migration already ran.

Skipping this step before restarting a service whose code expects a new
column (e.g. `patients.first_name`/`last_name`/`city`/`state`/`zip_code`,
added in migration 011) will make every request touching that column fail —
`intake-service` returns `503` from `_create_patient`'s
`except SQLAlchemyError` handler in that case. This was flagged in PR #19's
review and is exactly what `apply.sh` prevents.

**Rollback:** revert the code (`git revert`/redeploy the previous image).
Every migration in this repo so far only *adds* nullable columns, tables, or
indexes — nothing drops or renames existing columns — so rolling back the
application code is safe without rolling back the schema; the old code
simply ignores the newer columns it doesn't know about. If a future
migration ever needs to drop/rename something, write its own rollback note
here before merging it, and prefer an additive-then-backfill-then-drop
sequence over a single destructive migration.

**Exception — migration 009 touches data, not just schema** (PR #20 review):
adding the `insurance_coverages.status` CHECK constraint requires every
existing row to already satisfy it. If a real deployment has a row with a
status value outside `active | inactive | unknown | pending | stale`
(a manual repair, an old bug), 009 remaps it to `'unknown'` — but first
copies the original value into the new `status_legacy` column and raises a
Postgres `NOTICE` naming how many rows were affected, so the remap is
visible in deploy output and the original value stays recoverable. Check
`docker compose logs postgres` (or your deploy tool's captured output) after
running `apply.sh` for any `insurance_coverages_status_check: remapping ...`
notice, and review `SELECT * FROM insurance_coverages WHERE status_legacy IS
NOT NULL;` afterward — a non-null `status_legacy` means that row's status was
changed and should be checked by a human before trusting the new
`'unknown'` value operationally.

**Before applying migration 013 — check for duplicate-confirmed
appointments first (Stage 4, RIV-175):** migration 013 adds a UNIQUE index
guaranteeing at most one `'confirmed'` appointment per slot (the fix for
"charged twice"/two confirmations for one appointment). Its own preflight
check will **refuse to run** — `apply.sh`'s `set -e` then stops the whole
deploy — if any slot still has more than one confirmed appointment. This is
deliberate: an earlier version of this migration auto-cancelled the losing
row(s) based only on `created_at`/`id`, and a PR #20 review correctly
flagged that as too consequential to do silently — cancelling a real
confirmed appointment is a patient-facing state change that deserves human
review, not a migration-time heuristic.

If `apply.sh` stops with an `appointments_confirmed_slot_unique: N slot(s)
still have more than one confirmed appointment` error: run
`db/migrations/scripts/reconcile_duplicate_confirmed_appointments.sql` —
**read its header first** and review the flagged appointments with clinical
ops/billing before running its `UPDATE` (it reclassifies every
non-earliest confirmed row per duplicated slot to `'cancelled_duplicate'`,
stamped with `reconciled_duplicate_of` — nothing is deleted, but whoever
was counting on the cancelled appointment needs to be told). Re-run
`apply.sh` afterward; migration 013's preflight will pass once no slot has
more than one confirmed appointment left.

**Before enforcing migration 014 on any environment with real existing
patients (Week 4 catch-up, RIV-201):** migration 014 adds
`patient_access_grants` — the table `services/records-service/
patient_access_gate.py`'s `SqlPatientAccessGate` checks before serving
`GET /patients/{id}`, `GET /patients/{id}/records`, or `GET
/patients/{id}/view`. The table ships **empty**; only the demo seed
(`db/seed/generate_seed.py`) populates rows. Applying `apply.sh` alone
against a real environment does **not** create any grants — every
existing staff account would be denied every existing patient's chart the
moment this code deploys, with no in-app way to add a grant.

This is deliberate, not an oversight: there is no existing digital signal
in this schema (no care-team table, no per-patient assignment — see
`docs/analysis/RIV-201-patient-records-IDOR.md` §6) that could be used to
correctly auto-infer which staff member should be granted which patient.
Guessing (e.g. "grant every active user every existing patient" as a
migration-time default) would silently defeat the fix this migration
exists to deliver — the same reasoning `adr/0004-master-patient-index-
match-key.md` used to rule out auto-merging duplicate patients rather
than flagging them for review.

**Required rollout step, before this code reaches any environment with
real patients:** populate `patient_access_grants` for real user/patient
relationships as an explicit, reviewed operational decision — e.g.:

```sql
-- Example only — replace with your actual staff/patient assignment
-- decision. Granting broadly here just re-creates the pre-fix "any staff
-- can see any patient" posture; the point of this step is to make that a
-- reviewed choice, not a byte-for-byte revert of what this migration closed.
INSERT INTO patient_access_grants (username, patient_id)
SELECT u.username, p.id
FROM users u CROSS JOIN patients p
WHERE u.role = 'staff' AND u.is_active
ON CONFLICT (username, patient_id) DO NOTHING;
```

If a deployment cannot complete this step immediately, do not apply
migration 014 (or deploy this code) to that environment yet — an empty
grant table is a hard denial, not a soft/partial one, for every route it
protects.

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

`logs/intake-service.log` records one line per `/intake` request:
`POST /intake summary=<json>`. That JSON is built by
`_intake_log_summary` (`services/intake-service/app.py`) from an
**allowlist**, not a redacted copy of the request — it contains exactly two
keys, `correlation_id` and `created_via`, and nothing else, regardless of
what fields exist on the request schema today or are added later. An
earlier version of this fix ran the whole request body through
`libs/safe_logging.redact()` (a blocklist), but that missed several fields
across three separate review rounds before the allowlist replaced it
entirely — see `services/intake-service/app.py`'s `_intake_log_summary`
docstring for the full history. `redact()` itself is unaffected and remains
available as a general-purpose backstop for other code that logs structured
data; it just isn't what powers this particular log line anymore.

Insert **failures** on the patient/coverage/consent paths (`_create_patient`,
`_create_coverage`, `_record_consents`) log only the exception's type name
(e.g. `error_type=IntegrityError`), never `str(exc)` — a real SQLAlchemy
error's string form embeds the failed statement's bound parameters
(name/dob/ssn/address/... for patients, member_id/group_number for
coverage) unless avoided. The intake-service database engine is also
configured with `hide_parameters=True` (`services/intake-service/db.py`) as
a second, engine-level layer of defense against any other call site that
logs a SQLAlchemy exception directly.

**None of this retroactively scrubs log entries written before these
fixes** — any `logs/*.log` file that predates them may still contain
plaintext PHI, including from the request-body and blocklist-redaction
versions of this line that existed briefly during development. Treat the
logs directory as sensitive regardless; do not copy it off the host. No
other service's logging has been audited/fixed by this work — only
`intake-service`'s `/intake` path (happy path and error paths).

## CI

`.github/workflows/ci.yml`: frontend build, per-service import smoke, unit tests
(`pytest -m "not integration"`), then `docker compose build`. There is no
secret-scan, dependency-vuln-scan, or image-scan step — another known gap.
