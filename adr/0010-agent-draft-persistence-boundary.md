# ADR 0010 — Persisting the agentic draft, and where the prohibition actually applies

**Date:** 2026-08-21
**Status:** Accepted
**Context:** September 2 agentic patient-summary demo
**Related:** `adr/0008` (encryption at rest), `adr/0009` (AI enablement gate),
`db/migrations/018` (review gate), `db/migrations/020` (this table)

## Context

The client's privacy constraint reads: *persist only source ID/version, citation
IDs, categories, status, timestamps, correlation ID. Never persist prompts, model
output, retrieved text, patient data, identifiers, credentials, or raw provider
errors.*

Read literally, that forbids storing the generated draft — which is also the
thing the clinician must review and the patient must be shown.

The existing review gate does not have this problem.
`patient_summary_reviews` (migration 018) stores **no text at all**: an approval
points at a `record_id`, and the deterministic renderer regenerates the summary
at display time. That is safe *precisely because it is deterministic* — the same
record always produces the same words.

**A model response is not reproducible.** So the same design applied to an
agentic draft would mean the patient could be shown text no clinician ever
approved.

## Decision

**Persist the generated draft text as a dedicated clinical artifact, in
`agent_draft_provenance.generated_text`.**

The prohibition on persisting model output is scoped to **logs, traces,
telemetry, prompts and observability** — *not* to the clinical artifact that must
be reviewed and released.

Persisted per draft: generated text, source IDs and versions, citation IDs,
model ID and prompt **version**, validation state, reviewer, approval/rejection
state and timestamps, correlation ID.

### Required properties, and how each is enforced

| Property | Enforcement |
|---|---|
| Each draft is an **immutable version** | `BEFORE UPDATE` trigger `agent_draft_text_is_immutable` raises if `generated_text`, `version` or `patient_id` change. Status may still move (`draft → validated → approved`); the text may not |
| A revision is a **new version**, never an edit | `UNIQUE (patient_id, version)`; the trigger's message says so explicitly |
| Approval points at the **exact stored version** | `reviewed_by` + `approved_at`/`rejected_at` on the row itself, with a CHECK mirroring migration 018's decision-completeness constraint |
| Patient display **never regenerates** | Display reads the approved row's `generated_text`. There is no regeneration path |
| Draft text **never enters telemetry** | `libs/agent_provenance` raises `ForbiddenPayload` on `draft_text`, `summary_text`, `generated_text`, `content`, `text`, `response`, `completion`, `output` — enforced, not conventional |
| The **prompt** is not persisted | Only `prompt_version`. The prompt text stays out of the database exactly as it stays out of traces |

### Alternatives rejected

**Hash-only, regenerate at display.** Honors the constraint literally and fails
in practice: a model will not reproduce byte-identical output, so display would
refuse almost every time. A control that refuses the correct case is not a
control.

**Hold the draft in memory for the review turn only.** Approval could not
persist what was approved, so "display the exact approved version" becomes
impossible across requests.

**Option C — persist with an explicit telemetry exception.** This *is* the
chosen design; C and A differ only in whether the exception is written down.
It is written down: this ADR is that exception, stated deliberately rather than
left as an inference.

## Consequences

- ⚠️ **`generated_text` is PHI.** It must be included in the encryption-at-rest
  work (`w8-planner-2` B2, alongside `ssn`, `dob` and `notes`). Until that work
  lands, **this table stores unencrypted PHI**, and no document may claim
  otherwise.
- It must never be copied into a trace, a log, an analytics path, or a provider
  prompt without passing through `libs/deid`.
- The privacy claim narrows, honestly: *"model output is not persisted"* is
  **false** of this system and must not be said. The true statement is *"model
  output is persisted only as the reviewed clinical artifact, and never in
  telemetry."*
- `agent_draft_provenance` is **not** tamper-evident. The trigger prevents an
  in-place text edit; it does not prevent a `DELETE`, and there is no hash chain.
  Audit integrity is separate, unstarted work (`w8-planner-2` B3). Do not
  describe this table as protected.
- Retention is undefined. An immutable, append-only-by-convention table of
  clinical drafts grows without bound and has no documented retention period.

## What this ADR does not authorize

It does not authorize any claim of HIPAA compliance, production readiness, or
encryption at rest. This is a synthetic training project on local Docker
Compose. `generated_text` holds synthetic content only.
