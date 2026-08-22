# Riverbend Patient Portal — Operations Runbook

Practical "how do I run / fix this" notes for whoever is on call. Stack is Docker
Compose; one stack per clinic region.

## Required one-time setup: INTERNAL_SERVICE_TOKEN

Round-17 review (2026-08-06, PR #20), cycle branch 7A (2026-08-15) and
branch 7B (2026-08-20): `gateway`, `intake-service`, `records-service`,
`eligibility-service`, `scheduling-service`, `interop-service` and
`roi-service` all now refuse to start unless `INTERNAL_SERVICE_TOKEN`
is set to a real random value at least 32 characters long — this is the
shared secret that proves a call actually came through the gateway rather
than reaching a service directly.

Two distinct failures, both covered: docker-compose uses
`${INTERNAL_SERVICE_TOKEN:?...}`, so an entirely MISSING value stops compose
before any container starts; each service additionally checks the value at
startup, which is what catches one that is present but unusable (a
placeholder like `changeme`, or anything under the length floor). `.env.example`
ships this **empty on purpose** (a placeholder like `changeme` would be a
public, guessable secret every deployment shipped unmodified), so `.env`
needs it set explicitly before the first `make up`:

```bash
# generates a 64-char hex value; the SAME value is needed by all seven
# services — they share one .env, so setting it once here is enough
openssl rand -hex 32
```

Put that value in `.env`'s `INTERNAL_SERVICE_TOKEN=` line (see the detailed
comment above that line in `.env.example` for the full history). Without it,
the seven services fail fast at container startup with a clear `RuntimeError`
in `docker compose logs` (rather than starting and sitting "unhealthy" until
the healthcheck's retry budget runs out) — that log line is the signal to
come back here.

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
automatically (mounted into `/docker-entrypoint-initdb.d`), so a first boot
needs no seed command at all.

**`make seed` only works against an EMPTY database.** The seed file carries
explicit ids and no `ON CONFLICT` clauses, so running it against a database
that already has data fails every insert with a duplicate-key error — and
those errors scroll past looking like noise while nothing is actually
reloaded. To genuinely reload, drop the volume:

```bash
docker compose down -v && make up      # re-seeds from scratch on first boot
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

**Required rollout step (two-phase), before this code serves real chart
access:**

*Phase 1 — deploy closed.* Apply migration 014 and this code with the grant
table empty. Every chart route is deny-by-default; no one can read a patient
they hold no explicit grant for. This phase is safe to deploy on its own — it
removes the IDOR — but staff cannot open existing charts until Phase 2.

`db/migrations/apply.sh` only applies schema — it never blocks on data state,
so it succeeds the same way for a routine deploy, an intentional Phase-1
rollout, and a freshly seeded/demo database (PR #22 review round 4: an earlier
version ran this check unconditionally post-migration, which broke `make seed`
+ apply.sh in dev and could report a deploy as "failed" after the schema was
already mutated).

Before **promoting past Phase 1** — i.e. before relying on grant enforcement
against real existing patients — run the separate, explicit, opt-in check:

```bash
db/migrations/scripts/check_grant_coverage.sh
```

It counts patients with no **active** grant to an active user, using the exact
same predicate as records-service's gate (`revoked_at IS NULL`, not expired,
user `is_active`), so revoked/expired/partial rows don't count as coverage.
Exit 0 means every patient is reachable; a non-zero exit reports how many are
not, so you can distinguish "backfill incomplete" from "safe to enforce."

You don't have to remember to run it, though: records-service also runs the
identical check on every real process start (`app.py::
_check_patient_grant_coverage`). What happens if it finds an unreachable
patient depends on `ENVIRONMENT`:

- **`ENVIRONMENT=production`** — **refuses to start** (raises, uvicorn exits
  non-zero) rather than boot into a deploy that would deny every unbackfilled
  chart (PR #22 review round 6 — a warning alone didn't mechanically stop a
  bad deploy).
- **Anything else** (the default, including this repo's own `.env` and the
  seeded demo) — logs a **warning** with the unreachable count and continues,
  so `make up`/`make seed` keeps booting (round 4's lesson: don't hard-fail
  against the committed seed or a deliberately partial Phase-1 rollout).

Either way, this never disables enforcement itself — this codebase's own
authorization safety rules explicitly rule out an all-staff or administrator
bypass, so there is no "enforcement off" flag to flip; grant enforcement is
always deny-by-default. Backfilling grants (or running the coverage script
above) is how you resolve the warning/failure, in either environment.

*Phase 2 — populate grants from a reviewed source.* Insert only the specific
user/patient relationships that are actually justified, keyed on `users.id`
(never username), as an explicit, reviewed decision. From here on, front-desk
registration grants the registrar their new patient automatically
(`intake-service`), so Phase 2 is a one-time backfill for patients that
already existed before this deploy.

Do **not** grant broadly. A `CROSS JOIN users × patients` — or any "every
active staff member gets every patient" backfill — re-creates the exact
"any staff can see any chart" posture this migration exists to close. It is
not an acceptable shortcut. Derive grants from a real, reviewed assignment
signal and confirm the result before enforcing, for example:

```sql
-- Minimum-necessary starting point: grant each provider the patients they
-- have actually treated (encounters.provider ~ the provider's name), keyed on
-- users.id. This is an ILLUSTRATION of a reviewed, per-relationship backfill —
-- NOT every staff member every patient. Replace the join with your clinic's
-- real assignment source and review the rows before running.
INSERT INTO patient_access_grants (user_id, patient_id)
SELECT DISTINCT u.id, e.patient_id
FROM users u
JOIN encounters e ON e.provider = u.full_name
WHERE u.is_active
ON CONFLICT (user_id, patient_id) DO NOTHING;
```

Anything not covered by such a reviewed signal stays denied until an explicit
grant is added — that residual is the point, not a gap. A deployment that
cannot complete a reviewed Phase 2 can still run Phase 1 safely: an empty grant
table is a hard, correct denial for every protected route. Grants are revoked
by setting `revoked_at`, and can carry an `expires_at`; disabling a user
(`users.is_active = false`) immediately blocks all their grants (the gateway
re-checks `is_active` per request and `SqlPatientAccessGate` joins it).

## Demo accounts

All seeded users share password `portal123`. Twelve carry the deprecated flat
`staff` role — `frontdesk`, `rdelgado`, `drnguyen`, `roiclerk`, `mokonkwo` and
so on — because migrating real accounts onto the nine-role grid is separate,
roster-gated work.

**One exception: `drkim` carries role `clinician`.** It is the only account
that holds `summary_review.decide`, so it is the only account that can reach
the review queue. Without it the clinician half of the demo is unreachable by
anyone. (Full list: `db/seed/generate_seed.py`.)

## Running the patient-portal demo

Three prerequisites, each of which silently breaks the demo if skipped.

**1. `INTERNAL_SERVICE_TOKEN` must be set.** Seven services refuse to start
without it and compose refuses to interpolate — see the top of this file.
Never commit the value.

**2. The database must be seeded from the CURRENT seed file.** The four
canonical demo patients — 1042 (Maria Gonzalez), 1737 (Priya Khan), 1738
(Thomas Johnson), 1739 (Aisha Taylor) — and their trends, appointments and
(for 1738/1739) pre-activated portal accounts exist only there. On a fresh
volume this is automatic; on an existing one, `docker compose down -v && make up`.

**3. Reset between rehearsals — the demo is not repeatable without it.**

```bash
make demo-reset
```

Review decisions are durable by design: a rejected record is never re-queued
and an approved one stays released. So every rehearsal, and every run of the
integration suite, consumes demo state. `make demo-reset` (2026-08-22, covers
all four canonical patients) returns 1042 and 1737 to invite-ready (no portal
account — the demo starts from "front desk issues a code"), restores 1738's
and 1739's pre-activated accounts to active if a rehearsal deactivated one,
and re-asserts every staff/clinician grant the four charts need. It prints one
row per patient:

```
 patient_id |      name      | portal_account | coverage | encounters | records | trend_results | appointments | pending_reviews |           active_grants
------------+----------------+----------------+----------+------------+---------+---------------+--------------+-----------------+-----------------------------------
       1042 | Maria Gonzalez | none           | active   |          4 |       4 |             2 |            5 |               0 | frontdesk, rdelgado
       1737 | Priya Khan     | none           | active   |          3 |       5 |             2 |            2 |               0 | drkim, frontdesk
       1738 | Thomas Johnson | patient-1738   | active   |          3 |       3 |             2 |            2 |               0 | drpatel, frontdesk, patient-1738
       1739 | Aisha Taylor   | patient-1739   | active   |          3 |       4 |             2 |            2 |               0 | drnguyen, frontdesk, patient-1739
```

`active_grants` is the column to read for each row. It lists a grant only when
it satisfies the *gate's own* predicate — the account active, the grant
unrevoked and unexpired — never merely that a row exists, because a revoked or
expired grant leaves the relevant queue/chart empty while the account looks
perfectly fine. A missing expected name there, `portal_account` reading `none`
for 1738/1739, or `trend_results` under 2 for any row, means the database
predates the current seed: re-seed with `docker compose down -v && make up`. A
real Bedrock call against 1737 also writes an immutable `agent_draft_provenance`
row that this reset never deletes (by design) — a genuinely virgin agent-draft
demonstration needs that fresh-volume re-seed, not merely a reset.

### Keep these two OFF the primary demo path

Both are correct behaviour that reads as broken beside the patient summary:

- **`/records` → "Generate AI chart view"** returns a record *count*
  (`"Patient X's seeded chart has N encounter(s)…"`), not a summary. It is the
  staff-facing agent path and its composer is a known placeholder; the
  patient-facing summary is a different, deterministic path. Show it only when
  deliberately discussing that limitation.
- **Intake's eligibility result** stays `pending` forever without a payer
  credential. That is the async design working correctly — the payer is not on
  the request path any more — but on screen it reads as unfinished.

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
# Through the gateway — authenticated, and what the portal itself uses:
curl -s localhost:8070/eligibility/jobs/<job_id> -H "Authorization: Bearer <token>" \
  | python3 -m json.tool

# Or directly, from INSIDE the container. eligibility-service no longer
# publishes 8072 to the host: it verifies no caller identity, so a
# host-reachable port made the gateway's authorization bypassable for it.
# See tests/test_compose_port_exposure.py.
#
# Note this uses python, not curl — the service images are python:slim and
# do not ship curl. (A `docker compose exec ... curl` here fails with an
# OCI "executable file not found", which reads like a container problem and
# is not one.)
docker compose exec eligibility-service python -c \
  "import urllib.request,json,sys; print(json.dumps(json.load(urllib.request.urlopen('http://localhost:8072/eligibility/jobs/'+sys.argv[1])),indent=2))" \
  <job_id>
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
# wait a few seconds, then re-check any job that was in flight. 8072 is not
# published to the host (see above), so this runs inside the container —
# and via python, since these images do not ship curl:
docker compose exec eligibility-service python -c \
  "import urllib.request,sys; print(urllib.request.urlopen('http://localhost:8072/eligibility/jobs/'+sys.argv[1]).read().decode())" \
  <job_id>
```
It should still exist and eventually reach a terminal state
(`succeeded`/`dead_letter`), never silently disappear.

### Switching the eligibility-assistant runtime
`ELIGIBILITY_AGENT_RUNTIME` (`.env`) selects `raw_bedrock` (default, no
framework), `langchain` (comparison spike), or `ollama` (feature-readiness
Stage 2 local demo — see the section below) for the
`POST /visits/{visit_id}/messages` chat endpoint. An unset or unrecognized
value fails closed (the service logs a `ValueError` and the endpoint
degrades to a safe "assistant unavailable" reply) rather than silently
picking a default. **No live Bedrock credential exists in this repo**
(`BEDROCK_MODEL_ID=changeme`) — expect every chat turn to return
`termination_reason=provider_error` with a generic "check manually" reply
until a real model id/region/credential is configured (see ADR 0005,
"Unresolved").

### Local Ollama setup for the intake-instructions demo (feature-readiness Stage 1)
`POST /intake/instructions` (services/intake-service/app.py, backed by
`libs/intake_instructions`) explains one new-patient wizard step in plain
language. It reads the same `LLM_PROVIDER`/`OLLAMA_*` vars as everything else
in `libs/llm_client` — see `.env.example`'s "Stage 1 (feature-readiness)"
note for the exact `OLLAMA_BASE_URL` value a container needs versus a
host-run process. With the repo's default `LLM_PROVIDER=fake`, this endpoint
always returns its deterministic per-step template — a real local model is
optional and only needed to see the AI-composed path:

```bash
ollama list                 # confirm a model is already pulled
ollama pull llama3.2:3b     # if not — pick any small instruct model you have
```

Set `LLM_PROVIDER=ollama` and `OLLAMA_MODEL` to that tag in `.env`, restart
intake-service, then use the intake page's "What do I need for this step?"
button. A stopped/unreachable Ollama server, or an unset/`changeme`
`OLLAMA_MODEL`, degrades to the same deterministic template rather than
failing the request — see `instructions_wiring.py::get_llm_client`. This is a
local development/demo dependency only, not a hosted production service —
see the Stage 1 PR's "Demo scope and known limitations" for what a real
deployment would still need.

### Local Ollama setup for the eligibility chat demo (feature-readiness Stage 2)
`POST /visits/{visit_id}/messages` (services/eligibility-service, backed by
`libs/eligibility_agent`) is a front-desk chat surface over the
`check_eligibility` tool. Set `ELIGIBILITY_AGENT_RUNTIME=ollama` in `.env` to
use a local model instead of the repo's default `raw_bedrock` (which has no
live Bedrock credential and always replies "assistant unavailable" here) —
it reads the same `OLLAMA_BASE_URL`/`OLLAMA_MODEL` vars as Stage 1's intake
assistant above; see that section for the model-pull steps and the
container-vs-host `OLLAMA_BASE_URL` distinction.

The chat is only reachable through an **appointment the logged-in user has
an active grant for** — from the portal, open Appointments and use "Ask
about eligibility" on one of your own patient's appointments. There is no
`visits` table in this system; the URL's `visit_id` is required to be a real
`appointments.id`, checked against `patient_access_grants` before the
gateway will proxy anything downstream (`services/gateway/
visit_authorization.py`) — a visit for a patient you have no grant for
returns 403, and neither `patient_id` nor `insurance_id` is ever taken from
the browser regardless of what it sends; both are looked up server-side from
the authorized appointment.

A stopped/unreachable Ollama server, an unset/`changeme` `OLLAMA_MODEL`, or
the model returning something that isn't a valid tool call/reply all
degrade to the runtime's existing safe "assistant unavailable" reply
(`agent_wiring.py::handle_visit_message`) — never a raw error to the
patient's chart or the front-desk user. This is a local development/demo
dependency only, not a hosted production service.

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
