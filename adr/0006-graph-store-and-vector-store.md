# ADR 0006 — Graph store, vector store, and orchestration runtime: stay on PostgreSQL

- **Status:** Accepted (Stage 1 of 3 — decision capture only; no code changes)
- **Date:** 2026-07-28
- **Author:** Week 8 AI persistence & orchestration hardening deliverable. Like
  ADR 0004/0005, this is not authored by Helix Digital Partners (the original
  contractor) — no internal Riverbend team name exists in this repo to
  attribute it to (see `CLAUDE.md`, "Unknowns").

## Context

- This system deploys as Docker Compose on **one VM per clinic region** with
  no platform team (`ARCHITECTURE.md`), and every service already shares a
  single Postgres credential with no per-service least privilege (`adr/0001`).
  Against that operating model, the binding constraint for any new data
  infrastructure is **PHI surface-area and where the authorization boundary is
  enforced — not scale.** Every net-new data store is a new PHI-at-rest copy,
  a new backup/retention/audit scope, and (per `adr/0002`) a new place plaintext
  PHI could live. Postgres is already the governed system of record; a heavier
  engine is only worth that cost against a concrete, named future need.
- **Graph store.** `libs/patient_view_agent/graph.py` (`PatientGraphReader`,
  Week 4) already builds the patient knowledge graph as a bounded, per-request,
  single-patient, depth-3 `Patient → Encounter → Provider → Record` projection
  over relational rows, then discards it. Its own module docstring documents
  three properties relevant to this decision: the read is bound at
  construction from an already-authorized `scope.patient_id` (no code path
  turns an arbitrary integer into a read); every row is re-checked against that
  scope, with a mismatch raising `CrossPatientEvidenceError` before it can
  reach output; and provider nodes are **projected** from `encounters.provider`
  free text (`projected=True`, with an explicit `provenance` string) — there is
  no foreign key to a providers table, and no `Provider → Record` authorship
  edge is asserted, because the schema cannot prove one. `.claude/skills/w4-deliverable-planner/SKILL.md`
  (its own "Recommended implementation") already reached the same conclusion in
  prose: typed Python objects and adjacency indexes over deterministic rows
  were chosen over Neo4j for Week 4 because they are faster to deliver,
  introduce no new service or provider lock-in, and keep the prototype
  reviewable, with Neo4j deferred as "a future option only if graph size,
  traversal depth, or cross-domain reuse later justifies its operational
  cost." That decision has never had a durable, numbered home — it exists only
  as prose in a planning skill and a recommendation deck.
- **Vector store.** `libs/rag_eval/similarity.py` is a pure-Python
  `cosine_similarity()`; its own module docstring states the eval corpus and
  gold set are "demonstration-sized... not intended to scale past this
  harness." `libs/rag_corpus/corpus.py`'s docstring states plainly that "a live
  Postgres read is future work, not in scope" for the corpus builder — Week 8
  is that future work. Today, embeddings persist only to a provider-tagged
  **disk cache file** (`libs/rag_corpus/embedding_cache.py`,
  `.cache/rag_embeddings`); there is no database persistence and no ANN
  retrieval path, so retrieval cannot demonstrate a single enforcement point
  for the `patient_id` scope the rest of this architecture depends on.
- **Orchestration runtime.** `libs/patient_view_agent/runtime.py`'s
  `run_patient_view()` is a framework-free, fixed state machine (`authorize →
  chart + graph specialists → evidence validator → composer → final
  validator`). `docs/analysis/W5-orchestration-framework-evaluation.md`
  (treated here as evidence only, per instruction — its analysis is not
  re-derived) already evaluated custom Python against LangChain and LangGraph
  on this exact workflow and recommends keeping custom Python as the default
  and rollback, adding LangGraph only as an optional, reversible comparison
  behind a shared runtime contract (mirroring the Week 3 pattern in
  `libs/eligibility_agent/runtime.py`'s `build_agent_runtime(name)`), and
  rejecting LangChain outright because its core value — model-driven tool/
  agent selection — is exactly what this security boundary forbids.
- None of these three decisions had a durable, numbered record before this
  ADR. Without one they are not discoverable, not revisitable on a stated
  trigger, and easy to relitigate from scratch each time someone asks "why
  isn't this a real graph database?"

## Decision

**Stay on PostgreSQL for all three layers. Two of the three tracks are
decisions, not builds; the only net-new infrastructure this deliverable adds
(Stage 2, not this ADR) is a single Postgres extension.**

### 1. Graph store: relational now; Apache AGE before standalone Neo4j

Keep the patient graph **relational** — the current bounded, per-request
projection (or, if traversal needs grow, a recursive CTE over the same
tables). Do **not** adopt Neo4j now. The workload is a bounded, single-patient,
depth-3 projection built and discarded per request, not multi-hop or
cross-patient analytics, and — concretely — provider nodes are already
projected free text whose edges the current schema cannot even prove; a
graph database would not make that provenance any more real. Running a second
database engine would add a new PHI-at-rest copy of chart data, a new
credential and BAA surface, and a new backup/restore/DR procedure to a stack
that already runs one Postgres instance per clinic region with no platform
team to operate a second engine.

If a concrete, named need for multi-hop graph queries or cross-domain reuse
appears (see revisit trigger below), the escalation path is staged:

1. **Apache AGE** (an in-Postgres openCypher graph engine, loaded as a
   Postgres extension) is the **first** escalation — it stays inside the
   already-governed, already-backed-up Postgres instance and credential model,
   so it adds a query language, not a new data store or new operational
   surface.
2. **Standalone Neo4j** is considered only if Apache AGE's traversal
   performance or feature set proves insufficient against that same named
   need. This is a strictly harder bar: a new service, a new credential, a new
   backup/DR path, and a new BAA-relevant PHI copy in a one-VM-per-region
   deployment with no platform team.

### 2. Vector store: pgvector, not a dedicated or cloud vector engine

Persist RAG-corpus embeddings in PostgreSQL using the **pgvector** extension
(implemented in Stage 2, not this ADR), rather than a dedicated or cloud
vector database. This adds zero new PHI-at-rest stores, keeps embeddings
on-prem consistent with the existing `EMBEDDING_PROVIDER=fake|ollama`
("No cloud") posture, and — decisively — lets the ANN search and the
`patient_id` scope predicate be **one filtered query on one row under the
existing Postgres ACLs**, so the authorization boundary stays in a single
enforcement point instead of splitting across two systems (Postgres for
identity/authorization, a separate vector engine for content). Keep the
existing pure-Python cosine path as the no-infrastructure default and
fallback, selected behind one interface, mirroring the Week 3 optional-runtime
shape (`libs/eligibility_agent/runtime.py`).

**This is retrieval-path defense in depth, not an authorization fix.** The
`patient_id` filter on the vector row is scoped narrowly to the RAG retrieval
path added in Stage 2. It is a distinct property from, and does **not**
remediate, the unresolved `RIV-201` gateway/records-service IDOR
(`docs/analysis/RIV-201-patient-records-IDOR.md`): `GET /patients/{id}/records`
still does not bind the caller's session to the requested `patient_id`, and
every account still maps to the single flat `staff` role
(`config/roles.yaml`) with no per-action authorization. Nothing in this ADR,
and nothing pgvector adds, changes that. If RIV-201 is fixed, the pgvector
scope filter should be revisited to confirm it composes with whatever
authorization model replaces the current one — but it does not stand in for
that fix today or at any point before then.

### 3. Orchestration runtime: custom Python default; LangGraph optional and reversible; LangChain rejected

Keep custom Python (`run_patient_view()`) as the **default and rollback**
orchestrator for the Week 4 patient-view supervisor. Add LangGraph only as an
optional, reversible comparison behind a shared runtime contract (Stage 3, not
this ADR), with **checkpointing off** — a checkpointer is a new PHI-at-rest
surface, which is disqualifying on its own regardless of any other tradeoff.
**Reject LangChain** as the orchestrator entirely: its flagship capability,
model-driven tool/agent selection, is exactly what this authorization boundary
forbids — every tool call in this workflow must be a fixed, allow-listed,
non-model-chosen dispatch. This mirrors and adopts
`docs/analysis/W5-orchestration-framework-evaluation.md`'s conclusion rather
than re-deriving it.

## Alternatives considered

- **Neo4j now, for the patient graph.** Rejected: no concrete traversal need
  exceeds a bounded depth-3 single-patient projection today, and a second
  database engine multiplies operational and PHI-custody surface in a
  one-VM-per-region deployment with no platform team. Revisit only on the
  named trigger below.
- **A dedicated or cloud vector database (e.g. a managed vector service) for
  RAG retrieval.** Rejected: it would split the authorization boundary across
  two systems instead of one filtered query under one set of Postgres ACLs,
  and it would add a new PHI-at-rest copy and BAA surface for what this
  system's corpus size does not require.
- **LangGraph as the default orchestrator now.** Rejected: no durable,
  human-review/checkpoint need exists yet, and a checkpointer is a new PHI-at-rest
  surface; per `docs/analysis/W5-orchestration-framework-evaluation.md`, the
  benefits justify a reversible spike, not a migration.
- **LangChain as the orchestrator.** Rejected outright: model-driven tool
  selection is incompatible with the fixed, allow-listed dispatch this
  authorization boundary requires.
- **Doing nothing (leaving all three decisions as undocumented prose).**
  Rejected: without a numbered ADR, the decisions are not discoverable, are
  easy to relitigate, and have no stated revisit trigger to test future
  requests against.

## Consequences

- The graph-store and vector-store decisions now have a durable, numbered,
  revisitable home instead of living only in a planning skill and a deck.
- No code, test, configuration, dependency, or database file changes in this
  stage — this ADR documents decisions Stage 2 (pgvector persistence) and
  Stage 3 (optional LangGraph runtime) will implement.
- Two of the three tracks (graph store, orchestration runtime) remain
  decisions with no new infrastructure. The only net-new infrastructure this
  deliverable will add, in Stage 2, is a single Postgres extension
  (`pgvector`) plus one additive migration and table — not a new service, not
  a new credential model.
- Does not fix, and must not be described as fixing, `RIV-201` (the gateway/
  records-service IDOR), the flat `staff` role, non-expiring sessions, or any
  other pre-existing documented debt (`ARCHITECTURE.md` §7; `CLAUDE.md`). The
  pgvector `patient_id` filter is defense in depth for the retrieval path
  specifically, described precisely in §2 above so it is not conflated with
  that unresolved authorization gap.

## Revisit triggers (per layer)

- **Graph store →Apache AGE:** a concrete, named requirement emerges for
  multi-hop traversal (more than the current depth-3 bound), cross-patient or
  cross-domain graph analytics, or provider/record relationships that need a
  real, schema-backed edge rather than a projected one. Escalate to Apache AGE
  first; do not skip directly to Neo4j.
- **Apache AGE → standalone Neo4j:** Apache AGE is adopted and, in production
  use, its traversal performance, Cypher feature coverage, or tooling proves
  insufficient against that same named need.
- **pgvector → a dedicated/cloud vector engine:** the RAG corpus outgrows what
  a single Postgres instance's ANN index can serve at acceptable latency for
  this deployment's one-VM-per-region topology, with that latency need stated
  concretely (not anticipated).
- **Custom Python → LangGraph as default:** a durable, production human-review
  or checkpoint/resume requirement appears that the fixed state machine cannot
  satisfy without effectively rebuilding LangGraph's checkpointing itself —
  and, at that point, a checkpointer's new PHI-at-rest implications are
  explicitly re-evaluated, not defaulted on.
- **LangChain:** no revisit trigger is defined. It is rejected as the
  orchestrator on a structural incompatibility (model-driven tool selection
  vs. a fixed-dispatch security boundary), not a scale or maturity gap that
  more evidence would resolve.

## Related

- `docs/analysis/W5-orchestration-framework-evaluation.md` — the orchestration
  runtime comparison this ADR adopts (§3, Recommendation), treated as evidence,
  not re-derived here.
- `.claude/skills/w4-deliverable-planner/SKILL.md` — the original prose
  graph-store recommendation this ADR gives a durable home to.
- `libs/patient_view_agent/graph.py` — the current bounded relational graph
  projection.
- `libs/rag_eval/similarity.py`, `libs/rag_corpus/corpus.py`,
  `libs/rag_corpus/embedding_cache.py` — the current in-memory/disk-cache
  retrieval path pgvector will sit behind (Stage 2).
- `docs/analysis/RIV-201-patient-records-IDOR.md` — the unresolved gateway/
  records-service IDOR this ADR's pgvector scope filter is explicitly
  distinguished from, per §2 above.
- `adr/0001` (monorepo/stack), `adr/0002` (data & compliance) — the shared-
  credential and plaintext-PHI context this ADR's "no new data store" bias is
  weighed against.
- Stage 2 (`db/migrations/010_pgvector_embeddings.sql`, not yet implemented)
  and Stage 3 (`libs/patient_view_agent/runtimes/langgraph_runtime.py`, not yet
  implemented) are the implementations this ADR authorizes; both remain
  separate, individually reviewable commits per
  `.claude/skills/langgraph-imp-planner/SKILL.md`.
