# Week 4 — Seeded Patient Knowledge Graph: Conceptual Schema

Repository snapshot: `main` @ `c43a5f1`, 2026-07-23. Documentation only — no
graph database, ORM model, migration, or service code is introduced by this
document. Line numbers below should be re-checked before Stage 2 implements
against them.

## 1. Purpose

Define, before any code is written, what the Week 4 patient-view graph
represents, what it does not represent, where its data actually comes from,
and where the authorization boundary sits. This exists specifically so the
graph is not accidentally overclaimed as a normalized clinical relationship
model when the underlying schema does not support several of the edges a
"patient knowledge graph" name implies.

## 2. Node types

| Node | Backing source | Identity | Notes |
|---|---|---|---|
| **Patient** | `patients` table (`db/schema.sql:28-41`) | `patients.id` (`SERIAL`) | Root of every traversal. One graph instance is bound to exactly one authorized `patient_id` at construction time (see §5). No cross-patient node ever appears in one graph instance. |
| **Encounter** | `encounters` table (`db/schema.sql:92-104`) | `encounters.id` | Has a real FK to `patients.id` (`encounters.patient_id`). This edge is a genuine relational fact. |
| **Record** | `records` table (`db/schema.sql:107-117`) | `records.id` | Has real FKs to both `encounters.id` (`records.encounter_id`) and `patients.id` (`records.patient_id`) — i.e., a record's patient linkage does not depend on walking through its encounter; both are stored directly. |
| **Provider** | `encounters.provider` (free text column) | Normalized string label, **not** a foreign key | See §3 — this is a projection, not a joined entity. |

## 3. Relationship semantics and what is/isn't a real edge

| Edge | Cardinality | Real FK? | Semantics |
|---|---|---|---|
| Patient → Encounter | 1 : N | Yes (`encounters.patient_id REFERENCES patients(id)`) | An encounter belongs to exactly one patient. |
| Encounter → Record | 1 : N | Yes (`records.encounter_id REFERENCES encounters(id)`) | A record belongs to exactly one encounter. |
| Patient → Record | 1 : N (derived, also directly stored) | Yes (`records.patient_id REFERENCES patients(id)`) | Denormalized but real: every record row independently states its patient, so this edge does not have to be inferred by transiting Encounter. |
| **Encounter → Provider** | N : 1 (claimed) | **No.** `encounters.provider` is a free-text `TEXT` column (`db/schema.sql:96`), not a foreign key to the `providers` table (`db/schema.sql:61-66`). | **This edge is a projection, not a stored relationship.** The graph will group encounters by exact string match on `encounters.provider` (e.g., `"Dr. Patel"`) to *display* a Provider node. It does not verify that string against `providers.name`, does not resolve typos/variants, and cannot detect two different real providers who happen to share a name or one provider recorded under two spellings. **Any Provider node the prototype shows must be labeled "as recorded on the encounter," not "the provider of record."** |
| **Provider → Record (authorship)** | Not modeled | **No path exists.** `records` has no `provider_id`, no `author`, no `created_by` column at all (`db/schema.sql:107-117`). | **Record authorship cannot be established from this schema, full stop.** The prototype must not synthesize or display a "written by Dr. X" claim for any Record node. If a Provider node is shown alongside a Record, it is only because both happen to trace to the same Encounter — that is an encounter-level co-occurrence, not an authorship claim. |

Consequence: the graph has exactly three edges backed by real foreign keys
(Patient→Encounter, Encounter→Record, Patient→Record) and one node type
(Provider) that exists only as a same-encounter string grouping. Any diagram,
demo narration, or report language must keep that distinction visible — this
is a documented limitation carried from `ARCHITECTURE.md` §7 and the schema
itself, not something Stage 2/3 is expected to fix.

## 4. Evidence provenance

Every node and edge the graph reader returns must carry a stable evidence
identifier back to its source row, so the multi-agent composer (Stage 3) can
cite what it used instead of asserting free-text claims:

- Patient node → `patient_id` (matches `patients.id`).
- Encounter node → `encounter_id` (matches `encounters.id`).
- Record node → `record_id` (matches `records.id`).
- Provider node → the exact `encounters.provider` string value plus the list
  of `encounter_id`s that produced it (never a synthetic provider ID, since
  none exists).

An "evidence ID" in Stage 3's final structured response must resolve back to
one of these — `patient_id` / `encounter_id` / `record_id` — never to a
free-floating claim with no traceable row.

## 5. Authorization boundary

- The graph is a **read-only projection bound to a single, already-authorized
  `patient_id`** at construction time. There is no operation on the graph
  object that accepts a different `patient_id` — the object simply has no
  method that takes one as an argument after construction.
- Authorization is a **separate, prior step**, not a property of the graph
  itself. Stage 2 introduces an `AuthorizationPort` that must return "allow"
  for `(actor, patient_id, action, purpose)` **before** a graph reader or
  repository adapter is even constructed. A denial must short-circuit before
  any database access — see Stage 2's "deny before read" requirement.
- This authorization check is **new, Week-4-only code** guarding the new
  graph/agent path. It is not, and must never be presented as, a fix to
  `services/gateway/app.py:176-180` or `services/records-service/app.py:86-134`
  — those endpoints remain exactly as vulnerable as described in
  `docs/analysis/RIV-201-patient-records-IDOR.md` regardless of what Stage 2/3
  build. The real IDOR fix is separate, unscheduled work (see that document's
  §6).
- No model output (LLM tool call, generated query, generated patient ID) may
  ever supply or override the bound `patient_id`. The bound scope is a plain
  Python value set by code before any model is invoked, per this plan's
  "Recommended implementation" section.

## 6. Seeded-sample limit

- The graph is built from `db/seed/` deterministic fixtures only
  (`db/seed/generate_seed.py` → `db/seed/seed.sql`, backed by
  `db/seed/*.csv`) or from equivalent in-test fixture rows. It is never built
  from live production data, and Stage 2/3 introduce no new database
  connection beyond what a read-only fixture/repository adapter requires.
- The graph reader enforces explicit row and traversal caps (defined in
  Stage 2, e.g., a maximum encounter count and maximum record-per-encounter
  count per authorized request) so the prototype cannot be pointed at an
  unbounded chart and cannot be used to demonstrate anything beyond the
  seeded sample's scale. Exact cap values are a Stage 2 implementation detail,
  not fixed by this document.
- Per `CLAUDE.md`, no new sample PHI-like data (names, SSNs, DOBs) is
  fabricated for this graph — seeded patients 1042/1043 (`db/seed/patients.csv`)
  are the same demo identities already used in
  `tests/integration/test_records_flow.py` and referenced (sanitized) in the
  RIV-201 write-up.

## 7. Known upstream data-quality limitation (not in scope to fix)

Week 2's identity-fragmentation finding — self-service intake has no MPI/
match key (`intake.yaml: match_key: none`, `AUD-09`) — means a single real
person can exist as multiple `patients` rows with no link between them. The
graph operates strictly per `patient_id` and has no way to know that two
different `patients.id` values might represent the same person. This must be
stated explicitly in any Week 4 demo: the graph shows one patient row's
complete, correctly-scoped chart — it does not, and cannot, reconcile
duplicate-person fragmentation.

## 8. What this document does not do

- It does not define Python types, an `AuthorizationPort` interface, storage
  adapters, or test cases — that is Stage 2.
- It does not define the supervisor/specialist agent topology — that is
  Stage 3.
- It does not add, modify, or migrate any database object, service route, or
  dependency.
