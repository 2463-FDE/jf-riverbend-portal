# Responsibility matrix — training environment

**Date:** 2026-08-21 (Week 9 reconciliation pass: 2026-08-26) · **Status:**
operational ownership **not applicable**

> **Week 9 reconciliation, 2026-08-26.** This pass corrects PR/test claims
> that drifted stale since 2026-08-21 and updates blocker-card status against
> current repository evidence. It does not reopen or redo any completed work,
> and adds no implementation — only documentation. Substantial work has
> merged since the original date: the policy-corpus/policy-navigator delivery
> (PRs #70–#76, `libs/policy_corpus/`, `libs/policy_navigator/`) and the
> Week 6–8 closure slices (PRs #77–#79: HL7 comprehension + `adr/0011`,
> a golden-signal metric via `libs/metrics/`, and the data-flow/vendor memo
> `docs/planning/ai-data-flow-vendor-memo-week8-08-25-2026.md`). None of that
> is reopened here; it is reflected below only where it changes a claim this
> document already makes.

> **Scope decision, 2026-08-21.** This is a synthetic training project. There is
> **no production hosting target** — local Docker Compose only — and **no
> production operational handover or on-call ownership is in scope.** Bedrock is
> used solely as an external model-inference provider for synthetic-data
> testing; it hosts neither the application nor the database.
>
> `CODEOWNERS` and operational ownership are therefore **not applicable**, not
> merely unassigned. The rows below name the code area a future maintainer would
> read first, which is the part that remains useful.

Week 9 asks that outstanding items are merged or carry a blocker card with a
moved estimate and a named owner. In a training environment with no production
deployment, "owner" has no operational meaning: there is no on-call rotation to
join and no incident for anyone to be paged about.

**Two different things are being distinguished, and only one is waived:**

- **Operational ownership** — on-call, incident response, production escalation.
  **Not applicable.** There is no deployment, so there is nothing to be paged
  about. Every area row reads `N/A (training)` for this reason.
- **Repo / work ownership** — who picks a piece of open work up. **This is NOT
  waived.** Open engineering work exists (B-4, and B-2's simulation-mode item;
  B-6 is resolved as of this reconciliation pass) and someone must take it
  regardless of on-call. Every open card below therefore carries an explicit
  **Resolver** line. `unassigned training maintainer` is an acceptable
  resolver; silence is not.

The area column is retained because the code areas are real and a future
maintainer needs the map.

If this project were ever deployed for real use, every row would need a named
owner before that happened. That is recorded here as the condition, not as an
outstanding request.

## Areas

| Area | Repository surface | Owner | Notes |
|---|---|---|---|
| Gateway / authN + authZ | `services/gateway/` | N/A (training) | Session policy, RBAC grid loading, all outbound internal-token forwarding |
| Records + patient authorization | `services/records-service/` | N/A (training) | `patient_access_gate.py` is the RIV-201 control; the highest-risk file in the repo |
| Patient portal (invitation → summary → review) | `services/records-service/{patient_summary,review_queue}.py`, `frontend/app/{my-results,review-queue}` | N/A (training) | The purchased product |
| Intake + duplicate matching | `services/intake-service/` | N/A (training) | RIV-160 / `adr/0004` match key |
| Eligibility | `services/eligibility-service/`, `libs/eligibility_agent/` | N/A (training) | Async + circuit breaker. **Simulated** — no payer vendor or endpoint exists (B-2) |
| Scheduling | `services/scheduling-service/` | N/A (training) | Idempotency keys, booking constraints |
| HL7 interop | `services/interop-service/` | N/A (training) | Known gap: AL1/RXA dropped (`tests/test_hl7_parser.py` xfail). Comprehension report + proposed (not implemented) schema boundary: `docs/planning/hl7-segment-comprehension-week6-08-25-2026.md`, `adr/0011` |
| Release of information | `services/roi-service/` | N/A (training) | No signed-authorization check; no accounting of disclosures |
| Roster / role migration | `db/migrations/scripts/roster_dry_run.py`, `db/seed/staff_roster_SYNTHETIC.csv` | N/A (training) | Client roster received 2026-08-19 |
| Database schema + migrations | `db/schema.sql`, `db/migrations/` | N/A (training) | `schema.sql` is hand-maintained alongside forward migrations |
| Secrets + configuration | `.env.example`, `docker-compose.yml`, `adr/0007` | N/A (training) | `.env` untracked; template credentials blank. `PAYER_API_KEY` stays blank by decision |
| CI | `.github/workflows/ci.yml` | N/A (training) | No dependency, container or secret scanning |
| Deployment + operations | *nothing in repo* | N/A (training) | No deploy step, by design — local Docker Compose only (B-1) |
| Backup / recovery | *nothing in repo* | N/A (training) | Out of scope: no production data to recover (B-1) |
| Frontend | `frontend/` | N/A (training) | Nine screens |
| **LLM client** | `libs/llm_client/` | N/A (training) | Provider-swappable; timeout, retry/backoff, token/cost guard. **Stale as of 2026-08-21, corrected here:** `boto3`/`botocore` are now pinned in `services/eligibility-service/requirements.txt` and `services/records-service/requirements.txt` (bearer-token-capable `1.43.78`) — the "installed into no service container" root cause of Bedrock-call failures no longer holds for those two services as recorded in the tree; whether a live rebuild actually picks this up is Stage 5's job, not asserted here |
| **Patient-view agent** | `libs/patient_view_agent/` | N/A (training) | The deterministic evidence validator and composer. `composer.py` enforces cited ids ⊆ validated ids and never invents one — **the safety property the agentic demo depends on** |
| **Eligibility agent** | `libs/eligibility_agent/` | N/A (training) | Direct Bedrock Converse port with real tool use (`bedrock_tool_port.py`) and a bounded loop (`runtimes/raw_bedrock.py`). The September 2 demo's model transport |
| **RAG corpus / retrieval** | `libs/rag_corpus/` | N/A (training) | Corpus, pipeline, vector store, embedding cache. `rag_embeddings` is `VECTOR(16)`; `schema.sql` had drifted to 768 and is fixed — see `tests/test_schema_migration_parity.py` |
| **Retrieval eval** | `libs/rag_eval/` + `db/seed/goldset.json` | N/A (training) | Recall/precision harness behind the historical W2 report — five synthetic patient records, fake embedding provider. Superseded as *current policy-document* retrieval evidence by the policy-corpus row below; kept as the historical artifact, not reopened |
| **Policy corpus / navigator** | `libs/policy_corpus/`, `libs/policy_navigator/`, `docs/RagDocs/` | N/A (training) | Manifest-driven ingestion, real Bedrock Titan embeddings, pgvector retrieval, citation-ledger-gated LangChain navigator. Merged PRs #70–#76; Aug-24 client-corpus adoption + real-vector evaluation PRs #74–#76. Real-vector evaluation: 100% recall/citation accuracy over 10 runnable cases, `docs/planning/policy-rag-evaluation-08-25-2026.md` |
| **PHI-safe logging** | `libs/safe_logging/` | N/A (training) | Redaction, `PHISafeFilter` backstop. Matches dict KEYS only — cannot touch narrative, which is why `libs/deid` exists separately |
| **De-identification** | `libs/deid/` (merged, PR #52) | N/A (training) | Safe-Harbor scrub, 18 categories, tested. **Still wired to nothing** — confirmed again by the Week 8 data-flow memo (`docs/planning/ai-data-flow-vendor-memo-week8-08-25-2026.md`), which names the exact two future call sites and does not wire either. The control remains theoretical until that separate, later change lands |
| **Tracing / metrics** | `libs/tracing/`, `libs/metrics/` | N/A (training) | `new_correlation_id`, span wrapper. `record_exception_type` records the type, **not the message** — the privacy-safe pattern to follow. `libs/metrics/record_counter` (Week 7, PR #78) is the same pattern for a golden-signal counter — one metric wired (`policy_navigator_termination_total`), no monitoring platform added |
| **Role configuration** | `config/roles.yaml` | N/A (training) | The live RBAC grid, pinned by `tests/test_gateway_rbac.py`. `front_desk` deliberately lacks `records.read`; `default_role: staff` is declared and **read by nothing** |
| **Test suite** | `tests/` | N/A (training) | 1472 unit (`pytest -m "not integration"`) + 132 integration (`pytest -m integration`) collected as of 2026-08-26, up from 1023/95 on 2026-08-21 — collection counts only, not a rerun; see Stage 5 for the next real full-suite result. The evidence every claim in this handover rests on |

## Blocker cards

Three of the five cards previously here were **closed by scope decisions on
2026-08-21** rather than resolved by work. They are kept, marked closed, because
a card that silently disappears looks like it was done.

### B-1 · Deployment target — **CLOSED, not applicable**
- **Was:** blocking encryption-at-rest evidence, TLS termination, backup design
  and per-service database credentials.
- **Resolution:** there is no production hosting target. **Local Docker Compose
  only.** TLS, backup/recovery and production credential separation are
  therefore out of scope for this project, and must not be described as gaps
  against a deployment that does not exist.
- **Still true:** if this were ever deployed for real use, all four would be
  prerequisites.

### B-2 · Payer clearinghouse and BAA — **CLOSED, not applicable**
- **Was:** blocking the vendor-governance memo; eligibility cannot settle.
- **Resolution:** **no payer clearinghouse, live endpoint or real payer data
  exists in this simulation.** `PAYER_API_KEY` stays blank and eligibility
  behaviour is labelled simulated. **No BAA is executed, and none is represented
  as required for a real integration**, because there is no real integration.
- ⚠️ **Reconciles a contradiction with `adr/0007`. THIS CARD IS AUTHORITATIVE ON
  ROTATION.** `adr/0007:14` describes the disclosed `PAYER_API_KEY` as "a
  `pyr_live_`-prefixed payer clearinghouse key" and `adr/0007:57` instructs
  "Rotate **2026-08-21** with the payer vendor". Both were written before the
  2026-08-21 scope decision and are now wrong on one point: **the value was a
  fabricated simulation artifact, not a vendor-issued credential.** It matched a
  live key's shape, which is why it mattered for secrets hygiene, but it
  authenticated to nothing — `PAYER_API_URL` points at `edi.example.com`, an
  IANA-reserved placeholder with no endpoint behind it.
  **Therefore: there is no vendor to rotate with, and no rotation is possible or
  required.** The remediation that mattered is done — the key is untracked and
  blank. `adr/0007` is left unedited as the dated record of what was decided at
  the time; where it and this card disagree, this card governs.
- **Open engineering item, not a blocker:** confirmed still true on 2026-08-26
  — `services/eligibility-service/payer_client.py` still calls `httpx` against
  `PAYER_API_URL` (default `https://edi.example.com/...`, an IANA-reserved
  placeholder) with no simulation-mode guard. An eligibility check still
  attempts a real outbound request and fails/retries against it. That needs a
  simulation mode; it is work, not a client question.
  - **Resolver:** `OWNER UNASSIGNED` — unassigned training maintainer.
  - **Moved estimate:** 1–2 hours (a config-gated fixture response in
    `payer_client.py`, no new dependency).

### B-3 · Operational ownership — **CLOSED, not applicable**
- **Was:** no owner named for any area.
- **Resolution:** no production operational handover or on-call ownership is in
  scope for a training environment. `CODEOWNERS` is not applicable.

### B-4 · Runtime verification — **OPEN**
- **Resolver:** `OWNER UNASSIGNED` — unassigned training maintainer.
- **Moved estimate:** 2–3 hours. Everything below exists; none of it is written.

**Prerequisite for all three:** a locally-set `INTERNAL_SERVICE_TOKEN` (absent
from the committed template by design), then

```
docker compose build && docker compose down -v && docker compose up -d
```

A whole-stack rebuild is required, not a per-service one — a stale image against
a fresh schema produced 36 false failures on 2026-08-21.

| # | Command | Touches | Expected evidence | Recorded in |
|---|---|---|---|---|
| 1 | `pytest tests/ -m integration -q` (with `DB_PASSWORD`, `DATABASE_URL` exported) | `tests/integration/` — 132 collected as of 2026-08-26 (was 95 on 2026-08-21) | Pass/fail counts. Last recorded run (2026-08-21): **93 passed, 1 failed, 2 skipped**, failure = `test_eligibility_async_flow.py::test_visit_chat_endpoint_...`, HTTP 500 from `ModuleNotFoundError: No module named 'boto3'`. **Stale as recorded:** `services/eligibility-service/requirements.txt` now pins `boto3==1.43.78` (present as of this reconciliation pass) — the recorded root cause is likely fixed, but this has NOT been re-run against a fresh rebuild; that verification is Stage 5's job, not asserted here | This card, and the demo report |
| 2 | Per-metric, see the three rows below | the client's six metrics | A measured value or the word *uncaptured*. **Never an inferred value** | This card |
| 3 | `curl` as `frontdesk` against `/patients/1042/view`, `/review-queue`; as patient A against patient B | the four denial proofs | HTTP status **and** response body — the client asked for body-checked denials | The demo report's denial-proof section |

**The three uncaptured metrics** (three of six are already measured — services
verifying their caller = 7 of 7; front-desk derived-summary denial = **HTTP 200,
not denied**, recorded 2026-08-21 against the flat `staff` role model —
`config/roles.yaml` now enforces real least-privilege roles with `front_desk`
deliberately lacking `records.read`, so this specific recorded evidence
predates that change and needs re-checking, not assuming it's now fixed;
`default_role` readers = 0):

- *Released summaries carrying clinician approval* — run the review beat and
  count approved rows in `patient_summary_reviews`.
- *Summary source links resolving to authorized records* — follow each
  `source_record_ids` entry as patient and as front desk; the second must get
  nothing.
- *Synthetic A1C explanation: meaning + approved context* — **none exists.**
  The renderer is deterministic; record as uncaptured, not zero.

### B-5 · Week 8 scope — **RESOLVED — all three W8 artifacts complete (see B-6)**
- **Was:** the deliverables document assigns a Safe-Harbor de-identification
  scrub, a data-flow/BAA memo and a recommendation gate; the planning skill
  listed de-identification as out of cycle.
- **Resolution:** both tracks, minimal each. **The scope conflict itself is
  closed**, and — as of this reconciliation pass — so is the artifact gap
  B-6 tracked. All three W8 artifacts are now on `main`: the Safe-Harbor scrub
  (`libs/deid`, 18 categories enumerated, 26 tests) and the recommendation
  gate (`adr/0009`), merged via PR #52 (2026-08-21) — this card's prior "not
  yet on `main`" note was stale, corrected here — and the data-flow/vendor
  memo, merged via PR #79 (2026-08-25/26). See B-6.

### B-6 · Data-flow and vendor-governance memo — **RESOLVED**
- **Was blocking:** the third and final Week 8 artifact.
- **Resolution:** `docs/planning/ai-data-flow-vendor-memo-week8-08-25-2026.md`,
  merged via PR #79. Enumerates every real Bedrock call site (including the
  policy navigator's own Titan embedding call, not only its Converse call) and
  decides the future scrub boundary is exactly two caller-free-text fields;
  finds the patient-summary agent carries no per-patient facts to a model at
  all; finds no active analytics/export pipeline exists anywhere in the
  repository. Written to the simulation scope this card required — describes
  the pattern (a real-use PHI disclosure under 164.502(e) would require a
  BAA) without asserting one is executed or required for an integration that
  doesn't exist. Supplements, does not reopen, `adr/0009`.
- **Carried forward, unresolved by this memo (by design):** the scrub is
  still not wired into either named call site — that remains separate, later,
  testable work per `adr/0009`'s own gate item 1. The client's premise that a
  de-identified export already existed is **not supported by the
  repository** — no de-identification code existed before 2026-08-21, and no
  active analytics export exists now either.

## What someone inheriting this on Monday needs first

1. `docs/runbook.md` — the stack does not start without a locally-set
   `INTERNAL_SERVICE_TOKEN`; that is documented, not a defect.
2. `README.md` states the actual posture. **PHI is not encrypted at rest and
   this system is not HIPAA compliant.** Earlier versions claimed otherwise;
   `adr/0008` records why, and `tests/test_compliance_claims.py` fails if the
   false claims return.
3. `adr/` 0001–0011 in order (0009–0011 added since 2026-08-21: the AI-
   enablement recommendation gate, the agent-draft persistence boundary, and
   the proposed-only HL7 segment-mapping schema boundary). Every decision
   that matters is there.
4. `db/seed/README-staff-roster.md` before touching the roster migration.
5. ~~The three disclosed secrets in `adr/0007` still need rotating.~~
   **Stale, corrected here:** per `adr/0007`'s own per-value schedule and
   B-2's reconciliation, none currently need rotation in this training
   scope — `PAYER_API_KEY` was a fabricated simulation artifact authenticating
   to nothing (B-2 is authoritative over `adr/0007` on this point);
   `DB_PASSWORD` rotation is deferred to an actual deployment, which per B-1
   does not exist; `SESSION_SECRET` has no effect until session signing
   exists. `adr/0007` is left unedited as the dated record; this item is
   corrected here rather than there.
