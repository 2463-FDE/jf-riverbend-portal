# Clinical Source-Priority Policy

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Policy ID | CLIN-SRC-PRIORITY-001 |
| Version | 1.0 |
| Status | Approved for Training Demo |
| Effective Date | August 1, 2026 |
| Owner | Clinical Governance / Health Information Management |
| Review Date | August 1, 2027 |
| Applies To | Patient Summaries, RAG Retrieval, Clinical Review |
| Topic | Source Authority and Conflict Resolution |

## 1. Purpose

This policy defines how the synthetic training system prioritizes clinical and educational sources when retrieving evidence for patient-facing summaries.

The goal is to prevent the AI system from resolving conflicting clinical information by guessing.

## 2. Core Rule

The model does not decide which source is authoritative.

Source priority must be defined by approved application policy and enforced outside the model.

When two sources conflict and the policy cannot deterministically resolve the conflict, the system must refuse the unsupported conclusion or route the issue for clinician review.

## 3. Source Classes

For the synthetic training demonstration, sources may be grouped into the following categories.

### Priority 1 — Final Patient-Specific Clinical Records

Examples:

- Final laboratory results.
- Finalized structured observations.
- Signed clinician documentation.
- Approved corrected results.

These sources are authoritative for patient-specific facts such as values, units, dates, and documented clinical statements.

### Priority 2 — Approved Clinical Policies and Procedures

Examples:

- Laboratory Result Release Policy.
- AI Summary Review SOP.
- Approved clinic workflow policies.

These sources govern how information may be processed, reviewed, and released.

They do not override patient-specific clinical facts.

### Priority 3 — Approved Patient-Education Material

Examples:

- Patient A1c Education Guide.
- General preparation or follow-up instructions.

These sources may explain general concepts but must not be used to invent a patient-specific diagnosis, interpretation, or treatment recommendation.

### Priority 4 — Administrative and Scheduling Policies

Examples:

- Appointment Scheduling Policy.
- Portal access instructions.
- Registration procedures.

These sources govern administrative workflows and should not be used as clinical evidence.

### Excluded Sources

The retriever must exclude sources that are:

- Draft.
- Rejected.
- Superseded when a current approved version exists.
- Unapproved.
- Outside the authorized audience or patient scope.
- Untrusted external content not approved for the workflow.

## 4. Patient-Specific Facts Take Precedence

When an approved educational guide and a final patient result both mention A1c:

```text
Patient result
→ source of the patient's value and date

Education guide
→ source of general explanation
```

The education guide must not replace or modify the recorded patient result.

## 5. Corrected and Superseded Records

When a source has been formally corrected:

```text
corrected final record
→ current authority

older record
→ historical provenance only
```

The system should not present the older value as the current result.

Where history is relevant, both versions may be cited with clear dates and statuses.

## 6. Conflicting Sources

If two sources of equal authority conflict and no deterministic policy resolves the conflict:

```text
conflict detected
→ do not choose one
→ do not average
→ do not infer
→ clinician review
```

The AI must not resolve ambiguity based on wording, recency guesses, model confidence, or source similarity score alone.

## 7. Retrieval Rules

A bounded retriever should enforce:

- Approved status.
- Correct patient or audience scope.
- Allowed source categories.
- Maximum result count.
- Source identifiers and versions.
- Read-only access.

Approval status and authorization constraints should be enforced structurally and should not be exposed as model-controlled tool arguments.

## 8. Citation Rules

Every patient-facing factual statement generated from retrieved evidence should be traceable to an approved source.

A citation should identify enough metadata to locate the source without placing raw clinical content into telemetry.

Example metadata:

```text
source_id
source_version
source_category
citation_id
```

## 9. Computation Rules

Simple deterministic computation may be allowed when:

- Both values come from authoritative patient-specific records.
- The values represent the same analyte.
- Units are compatible.
- The computation is explicitly permitted.

Example:

```text
Previous A1c: 7.5%
Current A1c: 6.2%

7.5 - 6.2 = 1.3 percentage points
```

The computation does not authorize additional interpretation such as:

```text
"improving"
"controlled"
"normal"
"good"
```

unless an approved source explicitly supports that statement.

## 10. Source Priority Does Not Replace Authorization

A highly authoritative source is still unavailable if the requester is not authorized to access it.

The order is:

```text
Authorize
→ retrieve approved sources
→ apply source priority
→ generate/compute
→ validate
```

not:

```text
retrieve everything
→ choose best source
→ check authorization later
```

## 11. Fail-Closed Rule

If the system cannot determine:

- authorization,
- approval status,
- source version,
- source priority,
- or conflict resolution,

it must withhold the unsupported conclusion and route the case for review when appropriate.

## 12. Training Limitation

**This is a fictional source-priority policy created for a synthetic software demonstration. It is not an official clinical policy and must not be used to determine authority, resolve conflicts, or make clinical decisions for real patient records.**
