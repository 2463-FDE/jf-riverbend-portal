# AI data-flow and vendor memo — Week 8 (2026-08-25)

**Status:** working-tree evidence; not yet reviewed or merged
**Purpose:** decide, precisely, where a future `libs.deid.safe_harbor` scrub
boundary belongs, before any wiring happens. This is a decision memo, not an
implementation — no call site is changed by this document. It supplements,
and does not reopen or rewrite, `adr/0009-ai-enablement-recommendation-gate.md`
(Accepted, 2026-08-21), which already states the scrub "is not yet wired into
the LLM or analytics paths" as a general finding. This memo names exactly
which paths, and which do not need it.

## Every real Bedrock call site in this repository

Not only chat/model human-message paths — every path that constructs a real
Bedrock request, including embeddings, is enumerated below so a reader
auditing this boundary doesn't find one this memo said didn't exist.

| Component | What reaches Bedrock | Free caller text? | Patient/client data risk |
|---|---|---|---|
| `libs/policy_navigator/runtime.py::run_policy_navigator` | `question` — a caller-typed policy question (records-service's `/policy/ask` path) — as a Converse `HumanMessage` | **Yes** | A caller could type patient-identifying text into a policy question by mistake (e.g. "does my policy cover [named condition] for [patient name]?"). The retrieved tool evidence itself is the static synthetic policy corpus only — zero patient data on that side. |
| `services/records-service/policy_navigator_path.py` → `libs/policy_corpus/retrieval.py::PolicyRetriever.retrieve` → `libs/policy_corpus/bedrock_embedding_provider.py` | The **same** `question` text, embedded via Titan (`invoke_model`) for the vector search **before** the Converse call above runs | **Yes** | Same risk as the row above — this is a second, separate Bedrock call carrying the identical caller text. A scrub applied only before the Converse call and not before this embedding call would leave one of the two calls still receiving raw caller text. |
| `libs/eligibility_agent/runtimes/{raw_bedrock,langchain_runtime}.py::handle_message`, entered via `services/eligibility-service/app.py::post_visit_message` → `agent_wiring.handle_visit_message` | `req.message` — a caller-typed chat message on a visit (eligibility-service's visit-chat path) | **Yes** | Same shape of risk: free chat text a patient or staff member types could contain a name, DOB, member ID, etc. |
| `libs/summary_agent/runtime.py::run_summary_agent` | A **hardcoded** instruction string, `"Summarise the approved guidance for this reader."` — never caller-supplied text | **No** | Structurally cannot carry patient data through the human message, by construction — there is no free-text field here for a caller (or this code) to put patient facts into. The `audience` parameter is a role-scope value (e.g. `"patient"`), not a patient identifier, and is never sent to the model. Tool-retrieved evidence is the same fixed, approved, synthetic guidance corpus (`libs/summary_agent/manifest.json`) `policy_navigator` uses — not per-patient labs, notes, or results. |
| `services/intake-service/app.py`'s intake-instructions endpoint → `libs/intake_instructions/composer.py::compose_intake_instructions` → `libs/llm_client` (`LLM_PROVIDER=bedrock` selectable, `libs/llm_client/client.py`) | `req.step` — one of a closed, validated set of wizard-step names (`schemas.IntakeInstructionsRequest`, `VALID_STEPS`) | **No** | The request carries no patient data at all by construction — not a caller-free-text field, a closed enum the schema validates before this handler runs (`app.py`'s own comment confirms this explicitly). No scrub target here. |

No component in this repository sends real per-patient clinical facts (lab
values, visit notes, diagnoses) to a model today. The patient-summary agent's
name is easy to misread as "summarizes a patient's chart"; it does not — it
quotes and does arithmetic on numbers *printed in the approved synthetic
guidance document itself* (see its system prompt's worked example), scoped by
audience, not by patient.

## Analytics/export paths

ADR 0009's Precondition 1 names "the LLM **or analytics** paths" as the
unmet-wiring condition. Searched this repository (`grep` across `services/`
and `libs/` for `analytics`/`export`) for any active outbound analytics or
export pipeline this system runs: **none exists.** The only code-level
"export" reference is `libs/rag_corpus/corpus.py`'s docstring, which
describes reading checked-in teaching fixtures "never the client's raw
patients/encounters export" — a one-time **historical input** used to build
this training environment's seed data, not an outbound analytics pipeline
this application operates today. This matches ADR 0009's own finding that
the client's "we have a de-identified export for analytics" premise was
never supported by the repository. With no active analytics path found, this
memo resolves ADR 0009's "LLM or analytics paths" scrub-wiring condition in
full — model-provider paths per the table above, and analytics by absence.

## The boundary decision

**Apply a future scrub only to caller-supplied free text, before its first
use** — specifically:

1. `policy_navigator`'s `question` parameter, at the point `services/records-service/policy_navigator_path.py` (or the gateway) receives it from the caller, before it reaches EITHER of its two Bedrock uses: the Titan embedding call inside `PolicyRetriever.retrieve` and the Converse call inside `run_policy_navigator`. Scrubbing before only one of the two would leave the other carrying raw caller text.
2. `eligibility_agent`'s chat message, at `services/eligibility-service/app.py::post_visit_message` (the concrete entry point `req.message` reaches before `agent_wiring.handle_visit_message` calls into the runtime).

**Do not apply it to:**

- the static policy/guidance corpus under `docs/RagDocs/` or
  `libs/summary_agent/manifest.json` — it contains no patient data by design
  (`docs/RagDocs/README.md`'s authority boundary), and scrubbing synthetic
  text that was never identifying would be security theater, not a control;
- `libs/summary_agent/runtime.py`'s fixed instruction string — there is
  nothing there for a scrub to remove;
- `services/intake-service`'s intake-instructions `req.step` — a closed,
  schema-validated enum, not caller-supplied prose;
- any `audience`/`workflow`/role-scope value — these are fixed enum-like
  strings, not caller-supplied prose.

This is the "exact... boundary" ADR 0009's Precondition 1 asks for before
wiring: two named call sites, not "the LLM path" in general.

## What remains explicitly not done here

- `libs/deid/safe_harbor.scrub()` is **not called** from either named site in
  this change. Wiring it in is a separate, later, testable change (ADR
  0009's own gate item 1: *"A test proving no unscrubbed payload reaches a
  provider call"*) — this memo only decides where that change would go.
- No BAA is asserted, executed, or required by this memo. Per ADR 0009's
  scope note, this remains a synthetic training project with no real patient
  data and no production deployment target; a BAA is a precondition **only
  if** real patient data were ever sent to a model, which does not happen
  today at either named call site either (a caller *could* type identifying
  text by mistake; the system does not itself carry patient records into
  these prompts).
- The vendor facts already established in ADR 0009 (Bedrock as an external
  model-inference provider, synthetic data only, no live payer integration,
  no BAA represented as executed or required) are unchanged and not
  restated here as new findings — this memo adds the data-flow specificity
  ADR 0009 named as still missing, and defers to it for everything else.

## Related

- `adr/0009-ai-enablement-recommendation-gate.md` — the recommendation gate
  this memo completes the data-flow evidence for; not reopened or edited.
- `libs/deid/safe_harbor.py` — the existing, tested, unwired scrub this memo
  decides the future call sites for.
- `docs/RagDocs/README.md` — the authority boundary establishing the policy
  corpus contains no patient data.
