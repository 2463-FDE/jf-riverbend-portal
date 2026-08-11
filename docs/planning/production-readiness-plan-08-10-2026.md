# Riverbend Production-Readiness Plan — Weeks 1-7 Gap Closure + Scale Hardening

**Date:** 2026-08-10
**Author:** Claude Code (session analysis), for review by Jorge Ferreira
**Scope:** Verify Weeks 1-7 client deliverables against actual repo state (not the narrative, not stale docs — the code, tests, and merged history), then produce a 3-stage plan to bring the system to production grade for a **500,000-patient / 1,000-staff** deployment. Week 9's LLM-provider cutover (Ollama → Bedrock) is explicitly **out of scope** here and left untouched — see "What we do not touch" below.

---

## 1. Methodology

Four parallel research passes were run directly against this repo (`git log --all`, `git merge-base --is-ancestor` against every listed branch, direct file reads of the actual implementation, not just doc claims). Every finding below is anchored to a file path or commit. Where a memory/doc claim conflicted with the code, **the code wins** and the doc is flagged as stale in §3.

## 2. Week-by-week status (ground truth, not the client-message narrative)

| Week | Promised | Status | Evidence |
|---|---|---|---|
| 1 | Production LLM client wrapper + PHI-safe logging + onboarding seam map + debt log (D1/D9/D3) | **Done, merged, tested** | [libs/llm_client/client.py](../../libs/llm_client/client.py), [libs/safe_logging/redact.py](../../libs/safe_logging/redact.py), `docs/planning/onboarding-seam-map.md`, `docs/planning/ai-readiness-debt-log-07-04-2026.md` |
| 2 | RAG retrieval eval harness (recall/precision, fragmentation) + MPI/match-key ADR | **Done, merged, tested** | `libs/rag_eval/`, `libs/rag_corpus/vector_store.py` (pgvector), `docs/planning/retrieval-eval-report-07-08-2026.md`, `adr/0004-master-patient-index-match-key.md` |
| 3 | Single-agent eligibility assistant (check_eligibility tool + visit memory) on async/circuit-breaker/degradation eligibility | **Done, merged, tested** | `services/eligibility-service/{breaker,payer_client,cache,jobs,worker}.py`, `libs/eligibility_agent/{eligibility_tool,memory}.py`, `adr/0005-eligibility-agent-runtime-and-resilience.md` |
| 4 | KG schema + multi-agent retrieval prototype + authorization at graph boundary + IDOR writeup + N+1 note | **Done, merged, tested — and the IDOR itself is now actually fixed**, not just written up | `libs/patient_view_agent/{authorization,composer,graph,specialists}.py`, `docs/analysis/RIV-201-patient-records-IDOR.md`, `docs/analysis/W4-records-N-plus-one.md`, `db/migrations/014_patient_access_grants.sql`, `services/records-service/patient_access_gate.py`, passing test: `tests/integration/test_records_flow.py:47` |
| 5 | RIV-175 spec package (problem scope, acceptance criteria, DB design, test vectors) — **spec only, per the original ask** | **Done — and then over-delivered**: the fix was actually implemented in code too | `docs/planning/W5-*.md`, `db/migrations/013_appointment_idempotency_and_uniqueness.sql`, `services/scheduling-service/book.py`, `tests/integration/test_scheduling_concurrency.py` |
| 6 | HL7 legacy-comprehension report + characterization tests + failing AL1/RXA test + schema-validated-mapping ADR | **Never done.** Real gap. | `git log --all` shows zero HL7-related commits since the original handoff. `.claude/skills/w6-deliverable-planner/SKILL.md` describes exactly this work and was never executed — its prescribed branches don't exist. "Week 6" effort instead went into unrelated records/intake UI (PR #19, #21, #22). |
| 7 | Structured tracing + golden-signals dashboard spec + one working alert + content guardrail on "the LLM summary" | **Partially done.** Tracing is real and wired in. Dashboard spec and alert **do not exist anywhere**. The guardrail pattern exists but on a different feature (intake-instructions phrasing selector) — **the "AI patient visit summary" referenced in the Week 7 client message was never built**; it was added and then explicitly reverted before Week 1 even started (`d0905a1`: "the board's AI ask is a forward deliverable; the handoff baseline carries no AI code"). | `libs/tracing/spans.py`, wired at `services/intake-service/app.py:311`; no dashboard/alert files anywhere in `git log --all` |

### Reconciliation note (important)
Weeks 1-5 are in genuinely good shape — better than the client-facing narrative suggests, since several "documented risks" (RIV-088, RIV-141, RIV-175, RIV-201, D1 PHI-in-logs) that CLAUDE.md and ARCHITECTURE.md still describe as *open* have actually been fixed in later catch-up branches (`feat/week1-5-production-catchup-20260804`, PR #20/#22/#23). **Week 6 and half of Week 7 are the real, unambiguous gaps** that need net-new work, not just doc corrections.

## 3. Stale documentation found (must be corrected, low effort, high trust cost if left)

- `ARCHITECTURE.md:99-100` — still claims scheduling is an unguarded check-then-insert race. It isn't; `book.py` + migration 013 fixed this in Week 5's implementation.
- `tests/README.md:30` — still claims "no tests assert IDOR is prevented." `tests/integration/test_records_flow.py:47` does exactly that and passes.
- `CLAUDE.md` "Known Risks / Debt" section — mixes still-open items (sessions never expire, no MFA, flat RBAC, shared DB credential, no CI scanning, ROI authorization) with items that are **now fixed** (D1 PHI logging, RIV-175, RIV-201, RIV-088/141 sync eligibility) as if all were still open.
- `services/intake-service/logging_config.py` docstring — still describes full-PHI-body logging at INFO; the code (`app.py:309`, `_intake_log_summary`) no longer does this.
- `.claude/skills/w6-deliverable-planner/SKILL.md` — describes a plan that was never executed; either execute it (Stage 2 below) or mark it superseded.

These corrections are folded into Stage 1 as a single low-risk documentation pass — no behavior change, just making the written record match reality before anyone (including a future engineer, or an auditor per `docs/handover/auditor-questionnaire.md`) makes a decision based on a stale claim.

## 4. Scale-readiness findings (500k patients / 1,000 staff)

Current seed/test data is **~2,000x smaller than target** (`db/seed/generate_seed.py` generates 255 patients). Nothing in this repo has ever been exercised near production scale. Concrete gaps, each with file:line:

1. **No indexes on almost every patient-scoped foreign key** — `encounters.patient_id`, `records.patient_id`/`encounter_id`, `appointments.patient_id`, `consents.patient_id`, `insurance_coverages.patient_id`, `roi_requests.patient_id`, `disclosures.patient_id`. `db/schema.sql` even comments inline that records search hits the body column with no supporting index (full scan). At 500k patients this turns routine per-patient reads into full table scans.
2. **N+1 query pattern, confirmed and deliberately left** in `services/records-service/app.py:86-134` (documented in `docs/analysis/W4-records-N-plus-one.md` as `DEBT D8`, "do not collapse to a join" — i.e., flagged, not fixed). Compounds directly with gap #1.
3. **No connection-pool tuning anywhere.** Every service's `db.py` calls `create_engine(..., pool_pre_ping=True)` with SQLAlchemy defaults (5 + 10 = 15 connections/process). Postgres itself runs the stock image with default `max_connections` (100). With 1,000 staff and 6 services × N worker processes, this ceiling is reachable well before 500k-patient query volume becomes the bottleneck.
4. **Single instance of everything, no resource limits.** `docker-compose.yml` has no `deploy.replicas`, no `resources.limits`, one Postgres, one Redis, one instance of each of the 6 domain services. There is no horizontal-scaling path in this repo today.
5. **Redis is unbounded.** Stock `redis:7`, no `maxmemory`/eviction policy, combined with the already-documented never-expiring sessions (`SESSION_TIMEOUT: never`). At 1,000 staff over a multi-month deployment this is unbounded growth with nothing capping it.
6. **No log rotation/retention** in `logging_config.py` — file logs grow unbounded at 1,000-staff request volume regardless of the PHI-redaction fix already in place.
7. **No load-testing tool, capacity-planning doc, or performance budget anywhere in `docs/`.**

## 5. RAG document corpus — what needs to be sourced/ingested for production

The RAG work that exists today (`libs/rag_corpus`, `libs/rag_eval`) retrieves over the patient's **own** encounter/record rows — it solves fragmentation/dedup, not general knowledge grounding. For the assistants already in production (intake-instructions, eligibility chat) and the ones implied by the roadmap (a future clinical/records assistant, an ROI assistant, an internal support copilot per Week 9's continuity ask), a **document corpus** — distinct from patient data — is needed. Recommended categories, why each is needed, and a licensing/governance flag where relevant:

| Category | Purpose | Sourcing note |
|---|---|---|
| **Payer eligibility policy manuals / Summary of Benefits & Coverage (SBC) docs**, per payer Riverbend contracts with | Grounds eligibility-assistant answers about *what a plan covers*, beyond the raw 270/271 accept/reject the payer API already returns | Obtain directly from each payer relationship; typically public per-plan PDFs, no licensing barrier |
| **X12 270/271 transaction-set reference** | Correctly interpret/explain eligibility-response codes to staff; needed if the eligibility assistant is ever asked to explain a denial code | X12/WEDI-administered standard — **licensing required** to redistribute the full spec; can summarize publicly-documented code lists without the licensed text |
| **HL7 v2.x segment specification (PID, PV1, AL1, RXA, etc.)** | Directly needed for the Week 6 catch-up (schema-validated mapping ADR) and any future HL7 assistant/support copilot | HL7 International publishes the v2 standard; check current redistribution terms before embedding raw spec text verbatim |
| **ICD-10-CM code descriptions** | If scheduling/records/billing surfaces diagnosis codes to staff or a future coding-assist feature | ICD-10-CM itself is public domain (CMS/NCHS) — safe to ingest directly |
| **CPT code descriptions** | Only if billing/scheduling surfaces procedure codes | **CPT is AMA copyrighted** — requires a paid license to ingest/redistribute; do not embed without one |
| **Clinic-authored patient education / FAQ content** | Expands the intake-instructions phrasing bank (currently a small hardcoded set in `libs/intake_instructions/composer.py`) without weakening the existing "select, don't generate" grounding pattern — the assistant should keep choosing from a larger *clinically-reviewed* set, never free-text | Must be authored/reviewed by Riverbend clinical staff, not fabricated — this is patient-facing medical guidance |
| **Consent, ROI, and disclosure-accounting policy text** (45 CFR §164.508 obligations) | Grounds a future ROI assistant; directly relevant since `docs/handover/auditor-questionnaire.md` shows staff currently cannot answer a real disclosure-accounting request | Internal legal/compliance-authored; ties to the still-open "ROI has no authorization/accounting-trail enforcement" debt item |
| **HHS Safe Harbor de-identification guidance (45 CFR §164.514(b)(2), 18 identifier categories)** | Reference doc for the Week 8 de-identification scrub's unit tests and policy documentation | Public HHS guidance, safe to ingest directly |
| **Internal engineering corpus**: ADRs (`adr/0001`-`0006`), `ARCHITECTURE.md`, `docs/runbook.md`, `docs/analysis/*`, Jira ticket text (`docs/handover/jira-tickets.md`) | Feeds the Week 9 "who does my staff ask in 3 months" continuity ask — an internal support/onboarding copilot grounded in the system's own real documentation | Already in-repo; just needs the stale-doc corrections in §3 done *before* ingestion, or the copilot will confidently repeat stale claims |
| **Appointment/scheduling policy** (cancellation windows, no-show rules) | Grounds a future scheduling assistant | Internal-authored |

Ingestion should reuse the pgvector store already built (`libs/rag_corpus/vector_store.py`) — no new infra needed, just new source documents and a per-document-type access-control tag (patient-facing vs. staff-only vs. engineering-only), since some of these (ADRs, Jira tickets) must never leak into a patient-facing assistant's context.

## 6. What we do not touch

Per explicit instruction, the LLM backend stays on **Ollama** through all three stages below; the Bedrock cutover is Week 9's job. This plan does not require any rework to enable that swap — `libs/eligibility_agent/runtimes/` already implements `raw_bedrock`, `langchain_runtime`, and `ollama_tool_port` behind the same `AgentRuntime` contract (confirmed in `services/eligibility-service` research pass), and `libs/llm_client/providers/` is already provider-swappable via `LLM_PROVIDER`. Nothing in Stages 1-3 should narrow that abstraction.

---

## 7. The 3-Stage Plan

### Stage 1 — Documentation truth + compliance-critical fixes (no PHI blast-radius growth without this)

**Goal:** stop shipping stale claims, and close the security/compliance gaps that get materially worse — not just theoretically worse — once real PHI for 500k patients and 1,000 staff accounts is in this system.

1. **Documentation correction pass** (§3): fix `ARCHITECTURE.md`, `tests/README.md`, `CLAUDE.md` Known-Risks section, `logging_config.py` docstring. Mark `.claude/skills/w6-deliverable-planner/SKILL.md` either "to be executed in Stage 2" or superseded.
2. **Session hardening**: replace `SESSION_TIMEOUT: never` with an actual idle + absolute TTL in Redis; add MFA enforcement (`config/roles.yaml: mfa_required: false` → true, plus an actual MFA challenge in the auth flow) — currently zero MFA exists for any of 1,000 staff accounts with `patients.write`/`records.write` permissions.
3. **RBAC beyond the flat `staff` role**: `config/roles.yaml` still grants every one of 1,000 employees `patients.write`, `records.write`, `billing.read`, `disclosures.read` regardless of job function (front desk vs. clinician vs. ROI clerk vs. scheduler). Design and implement least-privilege roles per function — this is the single highest-leverage fix for blast radius at 1,000-staff scale.
4. **Per-service least-privilege DB credentials** — replace the single shared `riverbend_app` Postgres credential (`adr/0001`'s named deferred work) with one role per service, scoped to only the tables that service needs.
5. **ROI authorization + tamper-evident audit trail** — `audit_logs` today is mutable request-dump logging; build the disclosure-accounting capability the auditor questionnaire shows staff already failing to produce. This is a real compliance exposure independent of scale.
6. **CI dependency/container/secret scanning** — none exists today; add before scale-up, not after.
7. **.env**: leave as-is per standing instruction (never edit/print it), but flag in the plan that it must not ship to any environment beyond local dev before go-live — that's a deployment-process decision, not a code change here.

**Acceptance criteria:** stale-doc claims match code; every staff account requires MFA; roles.yaml expresses at least front-desk/clinician/ROI-clerk/scheduler as distinct least-privilege roles; each service connects to Postgres with its own scoped credential; a disclosure-accounting report can be produced for a given patient; CI fails on a known-vulnerable dependency or exposed secret.

### Stage 2 — Complete the missed Week 6/7 deliverables

**Goal:** deliver the two client commitments that never happened, using the existing "spec-first, small-PR-stages" pattern already proven in Weeks 4/5/6-UI.

1. **Week 6, executed as originally scoped** (`.claude/skills/w6-deliverable-planner/SKILL.md` already describes this almost exactly): 
   - AI-augmented legacy-comprehension report on `services/interop-service`'s HL7 parser — what PID/PV1 map to today, confirm AL1/RXA are silently dropped.
   - Characterization tests pinning current parser behavior (so the later mapping change can't silently regress PID/PV1 handling).
   - One new *failing* test explicitly demonstrating the AL1/RXA drop (the existing `tests/test_hl7_parser.py:38` xfail already documents this gap but predates any Week 6 effort — promote it to a properly-labeled characterization artifact, don't just leave it).
   - An ADR proposing a schema-validated mapping for AL1/RXA. **Spec only** — no full rewrite, matching the original ask.
2. **Week 7, close the gap**:
   - Golden-signals dashboard spec (latency/traffic/errors/saturation) for the intake critical path, built on the tracing spans that already exist (`libs/tracing/spans.py`) — as a checked-in spec (Grafana JSON or equivalent), not just tracing instrumentation with nothing to look at.
   - One working alert rule (latency or error-rate SLO breach) wired to the existing spans/correlation IDs.
   - **Resolve the guardrail mismatch honestly**: there is no "AI patient visit summary" in this codebase — it was built and explicitly reverted before Week 1. Either (a) formally retire that Week 7 line item and redirect the existing guardrail pattern (`libs/intake_instructions/composer.py`'s select-don't-generate design) to cover the eligibility assistant's chat output too, since that's the closest thing to a patient/staff-facing generative surface actually in production, or (b) if the client still wants a visit-summary feature, that needs to be scoped as new work, not a guardrail retrofit on a feature that was never built. Recommend (a) unless the client confirms they still want the summary feature.

**Acceptance criteria:** an HL7 legacy-comprehension report exists with per-segment mapping/drop detail; a properly labeled failing test demonstrates the AL1/RXA gap; a mapping ADR is accepted; a dashboard spec file and one alert rule exist and reference real span/metric names; the Week 7 guardrail scope is reconciled with what's actually built, in writing, with client sign-off.

### Stage 3 — Scale hardening + load validation for 500k patients / 1,000 staff

**Goal:** prove the system holds up at target scale before go-live, not after.

1. **Indexing pass**: add indexes on every patient-scoped FK identified in §4.1 (`encounters.patient_id`, `records.patient_id`/`encounter_id`, `appointments.patient_id`, `consents.patient_id`, `insurance_coverages.patient_id`, `roi_requests.patient_id`, `disclosures.patient_id`), plus the records-body full-text search gap already commented in `db/schema.sql`.
2. **Fix the N+1 in `services/records-service/app.py:86-134`** — replace the deliberate per-encounter query loop with a single join, now that Stage 1's RBAC/least-privilege work and Stage 2's dashboard give visibility to confirm the fix doesn't regress the authorization checks layered on top of it (`patient_access_gate.py`).
3. **Connection pooling**: size `pool_size`/`max_overflow` per service against a real budget (service-processes × pool_size ≤ safe fraction of Postgres `max_connections`); introduce PgBouncer (transaction pooling) rather than just raising Postgres's connection ceiling, given 6 services × N workers × 1,000 potential concurrent staff sessions.
4. **Horizontal scaling path**: add `deploy.replicas`/resource limits to `docker-compose.yml` for stateless services, or — if this is going to an actual multi-VM/region deployment as `ARCHITECTURE.md` claims — document the real target (this repo currently has *no* deploy mechanism at all; that's a separate, still-unknown item worth raising with the client directly rather than guessing).
5. **Redis**: set `maxmemory`+eviction policy, and rely on Stage 1's session-TTL fix to keep key growth bounded.
6. **Log rotation/retention** for all services now that PHI redaction (Stage 1 confirmed already fixed) makes volume, not content, the remaining log risk.
7. **Load-testing harness**: generate a representative 500k-patient / multi-year-encounter-history seed set (scaling `db/seed/generate_seed.py`'s deterministic generator, not fabricating new PHI-like data per the standing rule), and run realistic 1,000-concurrent-staff-equivalent load against intake, records, scheduling, and eligibility paths. Capture before/after numbers for the N+1 fix and the indexing pass — this becomes the "before/after number said aloud" material Week 10 will want anyway.
8. **RAG corpus ingestion**: stand up the document corpus from §5 behind the existing pgvector store, tagged by access tier (patient-facing / staff-only / engineering-only), and re-run the Week 2 eval harness against the larger, tiered corpus to confirm fragmentation metrics still hold at scale.

**Acceptance criteria:** documented before/after query-latency numbers for the indexing + N+1 fixes at representative scale; a load test report at ~500k patients / target concurrent-staff load with no connection-pool exhaustion; Redis memory bounded under sustained load; a written capacity-planning doc covering the actual deploy-mechanism unknown (escalated to the client, not guessed).

---

## 8. Open questions to raise with the client (not guessable from this repo)

- Real target growth curve: is 500k patients / 1,000 staff day-one, or a 1-3 year target? Changes whether Stage 3's capacity numbers are sized for launch or steady-state.
- Actual encounter/record retention policy (drives row-count assumptions used in load testing).
- How code is actually meant to reach "production" — `ARCHITECTURE.md` describes a VM-per-region model but no deploy mechanism exists anywhere in this repo (pre-existing unknown, not introduced by this plan).
- Whether the client still wants the Week 7 "AI patient visit summary" as new scoped work, given it was never actually built.
- Payer relationships and licensing budget for X12/CPT reference material, before committing to ingest those into the RAG corpus.
