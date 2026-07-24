---
name: w4-deliverable-planner
description: Plan and implement Riverbend Week 4's security-first seeded patient knowledge graph and bounded multi-agent retrieval prototype, including the RIV-201 IDOR/HAR writeup, graph-boundary authorization, and N+1 analysis. Use when executing or reviewing the Week 4 deliverable one manual-commit stage at a time.
---

# Week 4 Deliverable Planner

## Purpose

Deliver a seeded `Patient -> Encounter -> Provider -> Record` graph and a small,
read-only multi-agent patient-view prototype. Make the RIV-201 cross-patient
records exposure the headline. Enforce authorization deterministically before
any graph or records retrieval; never let a model grant access or choose an
unscoped patient identifier.

Treat this plan as a repository snapshot from 2026-07-23 (`main`, `c43a5f1`).
Before implementing a stage, re-inspect the named evidence because line numbers
and working-tree state may have changed.

## Current repository state

### What exists

- `services/gateway/app.py:57-62,176-180` authenticates a Redis session but does
  not authorize the requested patient. The session contains username/role, not
  a trustworthy `session.patient_id` ownership claim.
- `services/records-service/app.py:86-134` assembles a chart. It first selects
  encounters, then selects records once per encounter: the documented D8 N+1
  path. The service has no caller identity or authorization dependency.
- `services/records-service/app.py:137-159` performs an unbounded `ILIKE` scan
  across record bodies. Do not expose it as an agent tool.
- `db/schema.sql:28-117` contains relational patients, encounters, records, and
  providers. Encounters store provider as free text; they do not reference the
  scheduling `providers` table, and records do not identify an author. Any
  Provider node in the prototype must therefore be a documented projection,
  not a claimed normalized production relationship.
- `db/seed/` provides deterministic demo data suitable for a bounded sample.
- `docs/handover/portal.har` contains successful records requests for seeded
  patient IDs 1042 and 1043. Use only sanitized method/URL/status evidence; do
  not copy authorization headers, cookies, response bodies, or PHI.
- `tests/integration/test_records_flow.py:41-50` has an expected-failure test
  showing a cross-patient request is not rejected. CI excludes integration
  tests (`.github/workflows/ci.yml:56-69`).
- Week 1 supplies `libs/llm_client` and `libs/safe_logging`. Week 3 supplies a
  bounded tool-loop contract, strict tool arguments, fake-provider tests, and
  optional LangGraph patterns in `libs/eligibility_agent/`. Reuse the patterns,
  not the eligibility-specific classes.

### What is partial or only proposed

- Untracked Week 4 design artifacts propose a controlled LangGraph workflow,
  an authorization-first boundary, and a Neo4j option. Treat them as design
  input, not implementation proof.
- Authentication exists; patient/object authorization does not. A prototype
  policy fixture can demonstrate allow/deny behavior, but it is not remediation
  of the gateway IDOR.
- A relational patient chart exists; no knowledge-graph module, graph query
  contract, patient-view coordinator, or Week 4 unit-test suite exists.

### What is missing

- A dedicated sanitized RIV-201 finding and safe HAR reproduction.
- A Week 4 graph-schema document and a seeded graph projection.
- A deterministic authorization port at the graph boundary.
- A bounded supervisor and fixed read-only specialists that return evidence
  identifiers rather than unrestricted clinical text.
- A dedicated N+1 note and query-count evidence for the proposed read model.
- Tests proving denial occurs before retrieval, cross-patient evidence is
  rejected, tool/step limits hold, and logs contain no raw PHI.

### Dependencies

- Depend on Week 1 safe model/logging seams and Week 3 bounded-agent patterns.
- Do not depend on Week 2 vector retrieval; the seeded graph can use exact,
  deterministic traversal. Identity fragmentation from Week 2 remains a data
  quality limitation and must be stated in the demo.
- Week 6 parser loss can make allergy/medication graph context incomplete.
- Production use is blocked by RIV-201, direct service exposure, flat roles,
  and missing access auditing even if this prototype passes its own tests.

## Recommended implementation

Use a controlled supervisor with at most three fixed, read-only specialists:
chart retrieval, graph traversal, and evidence validation/composition. Run a
deterministic policy gate before the supervisor. Bind every tool to an already
authorized patient scope; expose no model-supplied `patient_id`, URL, SQL, or
arbitrary tool name. Cap branches, tool calls, rows, elapsed time, and model
turns. Require evidence IDs in the final structured response.

Represent the small graph with typed Python objects and adjacency indexes built
from deterministic seed/fixture rows. This is preferred over Neo4j for Week 4:
it is faster to deliver, introduces no service or provider lock-in, and keeps
the prototype reviewable. Document Neo4j as a future option only if graph size,
traversal depth, or cross-domain reuse later justifies its operational cost.

Use a code-first supervisor as the default. A LangGraph adapter may be an
optional comparison only if its existing optional dependencies are already
available and it preserves the same contracts. Do not use a peer-to-peer swarm:
dynamic delegation increases PHI fan-out and weakens auditability without
helping this fixed workflow.

Keep the actual gateway IDOR fix separate from the prototype. The Week 4 agent
boundary is defense in depth, not a substitute for authorization in the gateway
and records service. Never describe the system as production-safe or compliant.

## Implementation stages

### Stage 1 - Security finding, graph contract, and performance baseline

**Stage goal**

Create the documentation package that defines the security boundary and the
small graph before writing prototype code.

**Problem addressed**

The current planner can encourage an agent layer before the known IDOR and N+1
behavior are made explicit. It also assumes graph relationships the schema
does not fully store.

**Features to implement**

- Add a sanitized RIV-201 writeup with HAR request metadata, source evidence,
  impact, safe seeded repro, containment, remediation sketch, and test gap.
- Add the conceptual graph schema, relationship semantics, cardinalities,
  evidence provenance, authorization boundary, and seeded-sample limit.
- Add the N+1 note with the current `1 + N` query path, proposed joined/eager
  read model, query-count validation plan, and explicit non-goals.
- State that Encounter-to-Provider is currently projected from free text and
  that Provider-to-Record authorship cannot be proven from the schema.

**Files likely to be created or modified**

- `docs/analysis/RIV-201-patient-records-IDOR.md`
- `docs/planning/W4-patient-knowledge-graph.md`
- `docs/analysis/W4-records-N-plus-one.md`

**Libraries or configuration affected**

None. Do not add a graph database, model SDK, environment variable, migration,
or service configuration in this stage.

**Security considerations**

- Extract only method, sanitized URL path, patient demo ID, and status from the
  HAR. Never reproduce tokens, cookies, headers, response bodies, or names.
- Describe authentication and authorization separately.
- Make backend remediation a release gate; do not imply the future agent check
  closes the existing direct endpoint.

**Tests and validation**

```bash
git status --short
rg -n "patient_id|IDOR|N\+1|select\(Record\)" services/gateway/app.py services/records-service/app.py db/schema.sql
jq -r '.log.entries[] | [.request.method, .request.url, (.response.status|tostring)] | @tsv' docs/handover/portal.har | rg '/patients/.*/records'
git diff --check
```

Review the documents against `Weekly-Deliverables.docx`, `ARCHITECTURE.md`,
`docs/handover/jira-tickets.md`, and both system audits. Do not run a live
cross-patient request.

**Definition of done**

- The IDOR is the headline and is reproducible using only seeded identifiers.
- The graph is small, conceptual, and honest about unavailable relationships.
- The N+1 path and proposed validation are unambiguous.
- No application, test, configuration, or database file changed.

**Manual demo**

Walk through the sanitized HAR rows, point to the missing gateway scope check,
draw the graph from the document, and contrast `1 + N` with the proposed bounded
read model.

**Mandatory boundary:** Stop. Tell the user Stage 1 is complete, list the files
and validation results, and prompt the user to review and create the Git commit
manually. Do not begin Stage 2 until the user explicitly confirms.

### Stage 2 - Deterministic authorization and seeded graph core

**Stage goal**

Build a framework-neutral, read-only graph core whose first operation is a
fail-closed authorization decision.

**Problem addressed**

The repository has chart data but no trustworthy graph boundary. A model must
not be able to turn an arbitrary patient number into a retrieval request.

**Features to implement**

- Define typed request, authorized-scope, graph-node/edge, evidence, and denial
  contracts under a new `libs/patient_view_agent/` package.
- Define an injected `AuthorizationPort` that evaluates actor, patient, action,
  and purpose. Supply a deterministic fake policy for tests/demo; do not infer
  authorization from the current flat role or invent `session.patient_id`.
- Build a seeded in-memory graph projection with explicit row and traversal
  limits. Bind the graph reader to the authorized patient ID at construction.
- Add a read-only repository adapter or fixture adapter that groups a single
  joined/eager result set into encounters and records without calling the
  existing N+1 endpoint.
  - **Review-finding fix (from `docs/analysis/W4-records-N-plus-one.md`):** the
    current `Encounter`/`Record` models (`services/records-service/models.py`)
    have NO SQLAlchemy relationship (`Record.encounter_id` is a bare `Column`),
    so `selectinload(Encounter.records)` will NOT work as-is. The adapter must
    either add an `Encounter.records` relationship first, or (preferred, no
    model change) issue an explicit
    `select(Record).where(Record.encounter_id.in_([e.id for e in encounters]))`
    second query and group in Python. Do not copy the `selectinload` sketch
    unverified.
- Return evidence handles and minimum-necessary fields; do not persist graph
  state, prompts, or record bodies.

**Files likely to be created or modified**

- `libs/patient_view_agent/__init__.py`
- `libs/patient_view_agent/contracts.py`
- `libs/patient_view_agent/authorization.py`
- `libs/patient_view_agent/graph.py`
- `libs/patient_view_agent/repository.py`
- `tests/test_patient_view_authorization.py`
- `tests/test_patient_view_graph.py`

**Libraries or configuration affected**

Prefer the standard library and existing Pydantic. No Neo4j, NetworkX, vector
store, migration, service route, or deployment change is required.

**Security considerations**

- **Review-finding fix (from `docs/analysis/RIV-201-patient-records-IDOR.md`):**
  RIV-201 spans BOTH `GET /patients/{id}` (`PatientDetail` —
  demographics/SSN/DOB/`notes`) and `GET /patients/{id}/records` (`PatientChart`
  — clinical content). Treat a patient's demographics and chart as ONE protected
  scope: reject any evidence whose `patient_id` differs from the bound scope
  regardless of which projection (detail vs chart) produced it, and don't let
  the demo imply only the records endpoint is affected.
- Deny before constructing a graph reader or invoking a repository.
- Reject evidence whose patient ID differs from the bound scope.
- Accept no raw SQL, URL, tool name, or patient ID from model output.
- Use `libs.safe_logging`; log correlation ID, outcome, counts, and error type
  only. Do not log actor tokens, query text, names, or clinical content.

**Tests and validation**

```bash
pytest tests/test_patient_view_authorization.py tests/test_patient_view_graph.py -q
pytest -m "not integration" -q
git diff --check
```

Test allow, deny-before-read, cross-patient edge rejection, missing/duplicate
nodes, provider-projection labeling, row/traversal caps, deterministic ordering,
single-read/query-count behavior, and PHI-safe logs.

**Definition of done**

- Unauthorized requests execute zero repository/graph reads.
- Authorized seeded requests return only same-patient nodes and evidence IDs.
- The graph has fixed limits and no new infrastructure dependency.
- All unit tests pass using fakes or deterministic seed-derived fixtures.

**Manual demo**

Run one allowed fixture request and one denied/cross-patient request. Show that
the denied request records no repository call and that graph output is bounded
and includes provenance IDs.

**Mandatory boundary:** Stop. Tell the user Stage 2 is complete, list the files
and validation results, and prompt the user to review and create the Git commit
manually. Do not begin Stage 3 until the user explicitly confirms.

### Stage 3 - Bounded supervisor and fixed retrieval specialists

**Stage goal**

Assemble the patient view through a small multi-agent topology without widening
the data or tool boundary.

**Problem addressed**

The deliverable requires a multi-agent prototype, but an autonomous swarm would
make access, termination, and evidence provenance harder to prove.

**Features to implement**

- Add one deterministic supervisor with a fixed state sequence:
  `authorize -> chart specialist + graph specialist -> evidence validator ->
  composer -> final validator`.
- Bind each specialist to one allow-listed read-only tool and the same authorized
  patient scope. Use at most three specialists and no peer delegation.
- Reuse Week 3's bounded-turn, strict-argument, safe-error, fake-provider, and
  optional-runtime patterns. Do not import eligibility-specific contracts.
- Require a structured final response containing a plain-language summary,
  evidence IDs, limitations, and escalation flag.
- Refuse on missing, contradictory, cross-patient, or unsupported evidence.
  Send clinical, eligibility/payment, ROI, and identity ambiguity to a human;
  never let the model approve those decisions.
- Add a small seeded demo entry point. Do not expose a production HTTP route.

**Files likely to be created or modified**

- `libs/patient_view_agent/runtime.py`
- `libs/patient_view_agent/specialists.py`
- `libs/patient_view_agent/composer.py`
- `libs/patient_view_agent/demo.py`
- `tests/test_patient_view_runtime.py`
- Stage 1 documents only if implementation evidence requires a correction

**Libraries or configuration affected**

Reuse `libs/llm_client` and `libs/safe_logging`. Keep the default coordinator
framework-neutral. If a LangGraph comparison is added, isolate it behind the
same runtime contract and existing optional requirements; do not make it the
only runnable path.

**Security considerations**

- Authorization remains code-owned and precedes all agent work.
- Enforce fixed tool allowlists, strict schemas, time/turn/tool/row caps, and
  evidence-patient equality after every specialist.
- Persist no raw conversation or chart body. Do not trace prompts or responses.
- A human reviews high-consequence clinical or payment output; routine
  same-patient read-only demo output may complete automatically.

**Tests and validation**

```bash
pytest tests/test_patient_view_authorization.py tests/test_patient_view_graph.py tests/test_patient_view_runtime.py -q
pytest -m "not integration" -q
git diff --check
```

Test successful fan-out, zero-tool denial, unknown-tool rejection, maximum-turn
termination, timeout/provider failure, evidence mismatch, missing evidence,
human escalation, deterministic fake-provider output, and safe logging.

**Definition of done**

- One controlled supervisor and no more than three bounded specialists assemble
  a seeded patient view with citations.
- An unauthorized patient request is denied before any retrieval.
- Failure and ambiguity produce a refusal/escalation, not a guessed answer.
- The prototype adds no production endpoint and is not represented as the IDOR
  remediation.

**Manual demo**

Run the seeded demo for an authorized patient-view question, then repeat with a
denied patient scope and a missing-evidence case. Show the evidence IDs, bounded
execution metadata, safe refusal, and human-escalation outcome.

**Mandatory boundary:** Stop. Tell the user Stage 3 is complete, list the files
and validation results, and prompt the user to review and create the Git commit
manually. Do not commit, push, merge, or open a pull request.

## Final verification

Run after all three manually committed stages are present:

```bash
pytest tests/test_patient_view_authorization.py tests/test_patient_view_graph.py tests/test_patient_view_runtime.py -q
pytest -m "not integration" -q
git diff --check
git status --short
```

Also verify:

- The sanitized HAR writeup contains no credentials, headers, cookies, response
  bodies, or PHI.
- Denial occurs before every records, graph, eligibility, or model call.
- Graph reads, tools, steps, rows, and time are bounded.
- Every composed assertion has evidence IDs or is explicitly unknown.
- Logs contain only safe metadata and error types.
- The demo uses deterministic seeded/fake data and no live provider or network.
- The current integration xfail is not presented as fixed unless separate
  backend remediation was explicitly authorized and implemented.

Prepare the demo with the allowed, denied, and escalation scenarios. State the
remaining limits: flat roles, direct service exposure, missing access audit,
identity fragmentation, HL7 AL1/RXA loss, and non-normalized provider links.

## Completion report

After all stages are implemented, produce a concise report with:

- What was implemented and the patient-view/security problem it addresses.
- Any deviation from this plan and why.
- Files modified and the reason for each.
- Exact tests/commands run and their real results; list anything not run.
- Detailed seeded demo steps and expected visible results.
- Remaining risks, especially that the graph-boundary check does not replace
  gateway/records-service authorization or access auditing.
- Recommended next work: remediate RIV-201 at the real service boundary, make
  the regression run in CI, then decide whether production graph infrastructure
  is justified.

Never create Git commits automatically. At every stage boundary, stop and wait
for explicit user confirmation that manual review and commit are complete.
