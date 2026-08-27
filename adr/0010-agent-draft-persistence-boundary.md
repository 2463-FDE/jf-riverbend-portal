# ADR 0010 — Persisting the agentic draft, and where the prohibition actually applies

**Date:** 2026-08-21
**Status:** Accepted
**Context:** September 2 agentic patient-summary demo
**Related:** `adr/0008` (encryption at rest), `adr/0009` (AI enablement gate),
`adr/0012` (PHI field encryption — extended to this table below),
`db/migrations/018` (review gate), `db/migrations/020` (this table),
`db/migrations/032` (encryption + updated guard trigger)

**Update, 2026-08-27 (w8-planner-2 P2, adr/0012 follow-up):** `generated_text`
is now AEAD-encrypted (migration 032, `libs/phi_crypto`) — the Consequences
section's warning that this table stored unencrypted PHI no longer holds;
see the new subsection below for what changed and what did not. The
"Required properties" table's immutability row also referred to the
`agent_draft_text_is_immutable` trigger, which migration 020 itself already
superseded with `agent_draft_provenance_guard_trigger` before this ADR was
last touched — corrected in the same pass, since it is the same table's
guard mechanism this update is already describing.

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
| Each draft is an **immutable version** | `BEFORE UPDATE` trigger `agent_draft_provenance_guard_trigger` raises if `patient_id`, `version`, `provenance_label`, `correlation_id`, `model_id` or `prompt_version` change, and (migration 032) if `generated_text` changes WITHOUT `generated_text_key_version` also changing in the same statement — a same-key change is a content edit and is rejected; a paired change (initial encryption or a future key rotation) is allowed, since the trigger has no key and cannot verify content stayed the same across a re-encryption itself — that guarantee is the re-encrypting script's job, not the trigger's. Status may still move (`draft → validated → approved`); the text may not, except via that one paired path |
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

- **`generated_text` is PHI and is AEAD-encrypted** (2026-08-27, `adr/0012`
  follow-up, migration 032, `libs/phi_crypto`) — created encrypted from the
  moment a draft exists (`services/records-service/agent_drafts.py::create_draft`
  never writes plaintext, even transiently) and decrypted only at
  records-service's own response boundary (`app.py::_draft_out`), after
  that route's own authorization check already passed. AAD binds each
  row's ciphertext to its own `(patient_id, version)` — the same immutable
  identity migration 020 already froze — so ciphertext from one draft can
  never be substituted for another's undetected. `generated_text_key_version`
  NULL alongside a non-NULL `generated_text` means a row predates this
  migration and awaits `db/migrations/scripts/encrypt_agent_draft_text.py`'s
  backfill; every row created by current code always has both set together.
  Key custody is environment-variable-provided (adr/0012's posture, not
  repeated here) — not KMS-backed.
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

It does not authorize any claim of HIPAA compliance or production readiness.
`generated_text` is application-layer encrypted (see Consequences above) —
that specific claim is now true — but this remains a synthetic training
project on local Docker Compose with environment-variable key custody, not
a production deployment, and `generated_text` holds synthetic content only.
