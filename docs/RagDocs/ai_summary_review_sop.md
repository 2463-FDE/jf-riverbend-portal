# AI Summary Review SOP

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Document ID | SOP-AI-REVIEW-001 |
| Version | 1.1 |
| Status | Approved for Training Demo |
| Effective Date | August 24, 2026 |
| Owner | Clinical Governance |
| Review Date | August 1, 2027 |
| Applies To | Clinicians Reviewing AI-Generated Patient Summaries |
| Topic | AI Summary Review and Release |

## 1. Purpose

This Standard Operating Procedure defines how a clinician reviews, approves, rejects, or requests regeneration of an AI-generated patient summary before it is released to the patient portal.

The purpose is to ensure that AI-generated content remains evidence-bound, clinically reviewed, version-controlled, and withheld from patients until explicitly approved.

## 2. Scope

This sample applies to synthetic AI-generated patient summaries created from approved clinical and patient-education sources.

It covers:

- Draft generation.
- Deterministic validation.
- Clinician review.
- Approval or rejection.
- Version control.
- Patient release.
- Audit and provenance.

## 3. Preconditions

A draft may enter the clinician review queue only when:

- The patient identity and authorization context have been established by trusted application logic.
- Retrieval was limited to approved, authorized sources.
- Required citations and source identifiers are present.
- Deterministic validation completed successfully.
- The draft has a unique version.
- The draft is stored as an immutable review artifact.

If any precondition fails, the draft must not be released for patient display.

## 4. Review Procedure

The reviewing clinician should evaluate the exact stored draft version shown in the review queue.

The reviewer should confirm that:

- Reported laboratory values match the cited source records.
- Units and dates are preserved correctly.
- Any arithmetic is correct and uses compatible values.
- Every substantive statement is supported by an approved source or permitted deterministic computation.
- The draft does not invent diagnoses, causes, medications, treatment recommendations, or clinical significance.
- Citations correspond to the statements they support.
- No unsupported interpretation is presented as fact.
- The wording is understandable for the intended patient audience.

## 5. Approval

A clinician may approve the draft only after reviewing the exact version that will later be displayed.

Approval must record:

- Patient identifier.
- Draft version.
- Review decision.
- Reviewer identifier.
- Timestamp.
- Correlation or trace identifier when available.

After approval, the patient portal may display only that exact approved stored version.

The system must not regenerate or rewrite the content after clinician approval.

## 6. Rejection

A clinician should reject the draft when it contains:

- Unsupported claims.
- Incorrect values, units, dates, or calculations.
- Missing or invalid citations.
- Clinical interpretation not supported by approved evidence.
- Unsafe, confusing, or misleading language.
- Content belonging to another patient or encounter.
- Any other material error that makes the draft unsuitable for release.

Rejected drafts must remain unavailable to the patient.

## 7. Regeneration

If regeneration is requested, the system must create a **new draft version**.

Example:

```text
v1 = rejected
v2 = pending review
```

The system must not silently modify `v1`.

A previously approved version may remain visible to the patient while a newer version is pending review.

The newer version replaces the approved version only after it is separately approved.

## 8. Patient Visibility Rules

The patient portal may display:

```text
Approved draft
→ visible
```

The patient portal must not display:

```text
Pending draft
Rejected draft
Unreviewed draft
Superseded unapproved draft
Validation failure
→ withheld
```

If no approved version exists, the portal should state that no approved summary is available.

The pending draft is clinician-only. Only an authenticated clinician or nursing/medical-assistant user holding `summary_review.decide` and an active patient grant may review or decide it. Approval applies to the exact stored version; rejected, pending, or superseded drafts remain withheld.

## 9. Refusal and Validation Failures

A refusal or validation failure is a safe outcome.

The system should not weaken deterministic validation merely to obtain a passing model response.

If a draft fails validation:

- Do not release it.
- Preserve privacy-safe failure metadata.
- Allow a controlled regeneration when appropriate.
- Do not expose raw provider errors or hidden prompts to the patient.

## 10. Provenance and Traceability

The system should retain privacy-safe metadata sufficient to reconstruct the lifecycle:

```text
request
→ retrieval
→ model call
→ deterministic validation
→ draft version
→ clinician decision
→ approved display
```

Permitted metadata may include:

- Correlation identifier.
- Source identifiers and versions.
- Citation identifiers.
- Model identifier.
- Prompt version identifier.
- Validation status.
- Draft version.
- Reviewer identifier.
- Approval status.

Telemetry must not retain raw prompts, raw model responses, retrieved source text, patient data, credentials, or raw provider errors.

## 11. Fail-Closed Rule

When the system cannot prove that a draft is authorized, validated, reviewed, and approved, the summary must remain withheld.

```text
Unknown state
→ do not release
```

## 12. Training Limitation

**This is a fictional SOP created for a synthetic software demonstration. It is not an official clinical procedure, does not establish a standard of care, and must not be used to review or release information for real patients.**
