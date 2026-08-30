# W10 Final Stage 7 — smoke evidence and closeout (2026-08-30)

**Status:** working-tree evidence at authoring time; open for review on
branch `w10-final/s7-smoke-closure`. **`9c9dd4f` (main, PR #111 merged) is
the evidence baseline this report was produced against** — every command
and smoke run below ran on a stack built from that exact commit plus this
branch's own two fixes (OBS-M02, OBS-N01), described in full below. This
report is amended once more when this branch's PR merges, recording the
actual merge commit.

This is a **local observability POC**, not production monitoring: no
remote-write, no long-term retention, no alert routing/paging, no HA, no
TLS, no Grafana RBAC — see PR #111's own closeout for that stack.

## 1. OBS-M02 — two services could not actually start (discovered here, fixed here)

Bringing up the full stack from a clean `main` (a prerequisite for this
stage's own smoke run, not itself the assigned task) surfaced a real,
previously undetected deployment defect: `scheduling-service`, `roi-service`,
and `interop-service` all crashed at container startup with
`ModuleNotFoundError: No module named 'libs'`.

**Root cause:** all three import `libs.*` (`libs.safe_logging`, and
Stage 6's `libs.metrics` for scheduling/roi), a shared package that lives
at the repo root — but their Compose `build:` stanza was still
`./services/<name>` (their own directory only), and the root `.dockerignore`
explicitly excluded those three directories from ever being copied into a
root-context build. The image built fine every time (`COPY . .` just
copies the service's own directory); the failure only happens when the
container actually starts and Python tries the import — invisible to CI
(`docker compose build` only, never runs a container) and to `pytest`
(`conftest.py`'s `load_module` imports directly from the repo checkout,
where `libs/` is on the path regardless of any Dockerfile). `interop-service`
has imported `libs.safe_logging` since Stage 3 — this bug predates Stage 6
and was never specific to the metrics work; Stage 6 only added two more
affected services.

**Fix:** `docker-compose.yml`'s build stanza for all three switched to
`{context: ., dockerfile: services/<name>/Dockerfile}` (matching
gateway/intake-service/eligibility-service/records-service's existing
pattern), each Dockerfile updated to `COPY services/<name>/ .` +
`COPY libs/ ./libs/`, and the root `.dockerignore`'s three-line exclusion
of these services removed. A new static regression test
(`tests/test_service_docker_build_context.py`) asserts every service that
imports `libs.*` anywhere in its own `.py` files builds from the repo root
and its Dockerfile actually `COPY libs/ ./libs/` — parametrized over every
service directory, so the next service that grows a `from libs...` import
without updating its build context fails this test immediately, rather than
crashing silently at container-start time.

**Verification:** `docker compose build scheduling-service roi-service
interop-service` succeeded; all three then started and reported `healthy`
alongside the other five services — confirmed via `docker compose ps`.

## 2. OBS-N01 — the legacy N+1 chart route: batched, not deprecated

Stage 7 sub-slice 4 asks whether `get_patient_records`'s deliberate N+1
read pattern (DEBT D8) should be batched or marked deprecated/deferred,
decided by live telemetry, not static inspection alone.

**Static evidence first:** `frontend/app/records/page.tsx`'s "Load records"
action calls `frontend/app/api/records/route.ts`, which proxies verbatim to
gateway's `GET /patients/{patient_id}/records` — the exact route this
counter measures. Its own comment (`IDOR (intentional teaching point):
... the backend performs NO ownership check`) is **stale** — DEBT D11/
RIV-201 was closed in the Week 4 catch-up (`patient_access_gate.py`); the
route has required a real per-(actor, patient) grant since then. The
frontend code comment was not updated when that fix landed.

**Live telemetry, on the exact merged revision:** with
`records_legacy_chart_n_plus_one_total` at 0 on a fresh container, a real
browser click of "Load" on the Records page (logged in as `drkim`,
patient 1042) incremented it to 1. This settles the decision: the route is
still actively used by the current product UI, so it is batched, not
deprecated.

**The fix:** `get_patient_records` now issues 2 queries total regardless of
encounter count — the same encounters query, then one
`WHERE encounter_id IN (...)` query for every record across all of a
patient's encounters, grouped back into the same
`PatientChart`/`EncounterWithRecords` response shape, same ordering
(encounters by id, records by id within each encounter), same
authorization, same error handling, same audit write, same counter
placement (after a successful audit write, per the earlier
RECORDS-COUNTER-BEFORE-AUDIT review fix). The counter's Prometheus name
(`records_legacy_chart_n_plus_one_total`) is kept unchanged so any
dashboard/alert referencing it stays valid — its HELP text and code
comments now describe it as measuring the batched path, not an N+1 one.

**What this does NOT fix — a separate, still-open item:** `ARCHITECTURE.md`
had bundled "N+1 + full-table scans" as one line item. Checking
`pg_indexes` on a fresh volume shows `records`/`encounters` carry only
their primary keys — no index on `records.encounter_id` or
`encounters.patient_id`. Both the old N+1 queries and the new batched query
are sequential scans over those tables; batching reduces the scan COUNT
from 1+N to 2 but does not add the missing patient-scoped indexes.
`ARCHITECTURE.md` is corrected to describe these as two separate items —
the N+1 query-count problem (resolved here) and the missing-index/
full-table-scan problem (still open, a separate migration).

**Regression coverage:** `tests/test_records_n_plus_one_metric.py` gained
two new tests — one proving the batched response's shape/ordering is
unchanged, one proving query count stays flat (compares a zero-encounter
request against a 5-encounter/15-record request and asserts the delta in
`db.execute()` calls is exactly 1, not 5). Existing counter-placement tests
(denied read, audit-failure) are unchanged and still pass.

**Live re-verification after the fix:** rebuilt and restarted
`records-service`; the same browser "Load" click for patient 1042 produced
byte-identical rendered output to the pre-fix run (same 4 encounters, same
records, same note text), and the counter incremented again — confirming
the batched path is behavior-preserving, not just unit-tested.

## 3. Two clean smoke runs, exact merged revision

Both runs used the real stack (`docker compose up -d`, ephemeral
locally-generated `DB_ADMIN_PASSWORD`/`PHI_ENCRYPTION_KEY_V1`/
`PHI_BLIND_INDEX_KEY_V1` exported to the shell only — never written to
`.env`, which stays untouched throughout), `make seed`, `make
phi-backfill`.

**Run 1 — patient 1042, browser (`drkim`) + API (`frontdesk`):**
- Records: browser "Load" click → 200, rendered chart, `records_legacy_chart_n_plus_one_total` 0→1.
- Booking: `POST /appointments` (slot 95001, Dr. Anil Patel) via the browser's own "Book" button → 201 confirmed, `scheduling_booking_outcomes_total{outcome="success"}` 0→1.
- ROI: `POST /roi/requests` → `POST /roi/authorizations` → `POST /roi/authorizations/1/review` (`decision=valid`) → `POST /roi/requests/33/fulfill` → 200, `disclosure_id=17`, `roi_fulfillment_outcomes_total{outcome="success"}` 0→1.

**Run 2 — patient 1737 (Priya Khan), same workflows:**
- Records: browser "Load" click → 200, rendered chart (2 encounters, 5 records across encounters/lab results) → counter 1→2.
- Booking: `POST /appointments` (slot 95002, Dr. Anita Nguyen) → 201 confirmed → counter 1→2.
- ROI: full create→authorize→review→fulfill cycle (recipient "Riverbend Insurance Co", request 34, authorization 2) → 200, `disclosure_id=18` → counter 1→2.

Every dashboard-relevant metric now has real, non-zero values: gateway
request rate/latency across every route hit above, records-service's
batched-path counter, scheduling's booking-outcome counter, and roi's
fulfillment-outcome counter — the 6-panel "Riverbend Services" Grafana
dashboard provisioned in PR #111 has genuine data to show for all six
panels, not just the two (request rate, latency/error/in-flight) that a
plain healthcheck ping would already produce.

## 4. AI model-ID evidence — unconfirmed on this fresh environment, not fabricated

Stage 5's own prior rehearsal (`docs/planning/w10-stage5-final-handoff-08-26-2026.md`,
commit `6c7fc49`) recorded a real, successful invocation returning
`model_id: "us.anthropic.claude-sonnet-4-6"`. Repeating that exact
rehearsal on this stage's fresh Docker volume produced a **different,
genuine result, not the same success**:

- `POST /patients/1738/agent-draft` → 200, `{"status":"refused",
  "provenance_label":"fallback","model_id":null,
  "validation_code":"REFUSED_NO_CLAIMS"}`. Cause: this fresh volume was
  never run through the RAG corpus ingestion step — `policy_chunk_embeddings`
  has 0 rows. Sub-slice 1's acceptance ("disabled/superseded/out-of-audience
  chunks cannot support a summary draft") extends correctly to "no corpus at
  all": the route refused rather than fabricating a citation.
- `POST /policy/ask` → 200, `{"label":"fallback",
  "termination_reason":"provider_error"}`. `records-service`'s own log
  shows the real cause: `error_type=AccessDeniedException` — a genuine AWS
  Bedrock authorization failure for the credential configured in this
  environment's `.env`, not a simulated or injected failure. No raw AWS
  error text reached the caller; the categorical `provider_error`
  classification and safe fallback text are exactly what Stage 3/4's error
  boundary is supposed to produce.

**Per the freeze-sheet reconciliation's own rule** ("records the actual
runtime model and flags a missing owner decision if approval is still
undocumented") **and the stage's acceptance criterion** ("label model
selection unconfirmed rather than changing it by inference"): no
successful model invocation occurred in this session's smoke run. Model
selection is recorded as **unconfirmed on this environment** — not
"resolved," not "us.anthropic.claude-sonnet-4-6 as previously observed."
Re-ingesting the corpus and/or provisioning a Bedrock-authorized credential
for this environment are both out of this stage's scope (Stage 7 does not
ask for corpus ingestion, and credential provisioning is an infrastructure/
account decision, not a code change).

## 5. Freeze-sheet items this stage closes

- **Exact-current-main rehearsal** — done (§3 above), on `main`+this
  branch's two fixes, superseding the `6c7fc49`-era rehearsal.
- **Model ladder** — no change made; still correctly not-inferred (§4).
  `BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6` remains the configured
  value; this session did not confirm it is the value actually returned by
  a successful call, because no call succeeded.
- **Retained correlated lifecycle trace / loop exhaustion truthfulness /
  current approved corpus on the summary path** — unaffected by this
  stage; Stage 4/5's own work stands, not revisited here.

## 6. What this report does not claim

- Not a production-readiness sign-off — this remains a local observability
  POC (Prometheus/Grafana/Loki/Alloy), explicitly excluding HA, TLS,
  Grafana RBAC, Alertmanager routing, and retained distributed tracing (see
  PR #111).
- Not a corpus-freshness or retrieval-evaluation report — §4 documents an
  unconfirmed/refused state on a fresh, un-ingested environment; it does
  not reassert Stage 5's retrieval-evaluation gate, which runs in CI
  separately against its own fixture corpus.
- Does not claim the missing-index/full-table-scan half of
  `ARCHITECTURE.md`'s former N+1 line item is resolved — only the
  query-count (N+1) half is (§2).
- Does not claim OBS-M02 was specific to Stage 6 — `interop-service`'s
  instance of it predates Stage 6 by several weeks (§1).

## 7. Final merge commit

To be amended once this branch's PR merges.
