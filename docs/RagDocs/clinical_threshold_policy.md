# Clinical Threshold Policy

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Policy ID | CLIN-THRESH-001 |
| Version | 1.0 |
| Status | Approved for Training Demo |
| Effective Date | August 1, 2026 |
| Owner | Clinical Governance |
| Review Date | August 1, 2027 |
| Applies To | Patient Summaries, Clinical Review, RAG Retrieval |
| Topic | Use of Clinical Thresholds and Reference Ranges |

## 1. Purpose

This policy defines how the synthetic training system may use clinical thresholds, reference ranges, and categorical interpretations in patient-facing summaries.

The goal is to prevent the AI system from inventing or inferring clinical meaning from a numeric result unless that meaning is explicitly supported by an approved source.

## 2. Core Rule

A laboratory value may be quoted exactly from an approved patient-specific source.

A clinical interpretation such as:

```text
normal
abnormal
high
low
critical
controlled
uncontrolled
improving
worsening
```

may be stated only when an approved source explicitly provides or authorizes that interpretation.

The model must not create its own threshold.

## 3. Source of Threshold Authority

Thresholds and reference ranges may come only from approved sources such as:

- Final laboratory result records containing a reference range.
- Approved clinical policies.
- Approved laboratory policies.
- Approved patient-education material that explicitly defines the relevant range.
- A clinician-authored and approved interpretation.

The model's training knowledge is not an approved threshold source.

## 4. Patient-Specific Results

For a patient-specific result, the system may display:

- Analyte name.
- Result value.
- Unit.
- Result date.
- Reference range when present in the authoritative source.
- Explicit source-provided status such as `HIGH`, `LOW`, or `CRITICAL`, when present.

Example:

```text
A1c: 6.2%
Reference range: supplied by source record
Result date: 2026-08-11
```

If the source record does not provide an interpretation, the system must not invent one.

## 5. Synthetic Demonstration Thresholds

The training corpus may contain fictional thresholds created only to demonstrate source-grounded behavior.

Any such threshold must be clearly labeled as synthetic.

Example:

```text
SYNTHETIC DEMO ONLY

Example analyte: DEMO-X
Reference range: 10–20 units
High: >20 units
Low: <10 units
```

These values are not clinical standards and must not be reused outside the training environment.

## 6. Computation Versus Interpretation

Simple deterministic arithmetic may be allowed when:

- Both values come from approved patient-specific records.
- They refer to the same analyte.
- Units are compatible.
- The calculation is explicitly supported by the workflow.

Example:

```text
Previous A1c: 7.5%
Current A1c: 6.2%

Difference:
7.5 - 6.2 = 1.3 percentage points
```

This supports:

> The recorded A1c decreased by 1.3 percentage points.

It does **not** automatically support:

```text
The patient's diabetes is improving.
The result is normal.
The result is controlled.
No follow-up is needed.
```

Those statements require separate approved evidence.

## 7. Conflicting Thresholds

If two approved sources provide conflicting thresholds:

```text
conflict detected
→ do not choose one
→ do not average
→ do not infer
→ route for clinician review
```

Source-priority rules may resolve the conflict only when an approved policy explicitly defines which source controls.

The model must not resolve the conflict based on similarity score, wording, recency guesses, or model confidence.

## 8. Missing Thresholds

When no approved threshold or reference range exists:

```text
value available
+
threshold unavailable
→ report value only
```

The system may say:

> The recorded result is 6.2%.

The system must not say:

> The result is normal.

unless an approved source supports that interpretation.

## 9. Critical Results

A result may be treated as critical only when the authoritative source or approved policy explicitly marks it as critical.

A model-generated judgment that a result "looks critical" is not sufficient.

Critical-result workflows must remain controlled by trusted application logic and approved policy.

## 10. Patient-Facing AI Summaries

AI-generated summaries must:

- Preserve exact values, units, and dates.
- Use only approved threshold sources.
- Cite the source supporting any categorical interpretation.
- Avoid unsupported clinical meaning.
- Pass deterministic validation before clinician review.
- Remain withheld until the exact version is approved when clinician review is required.

If the draft cannot support a threshold-based statement, it must omit or refuse that statement rather than guess.

## 11. Deterministic Validation

The validator should reject a draft when it:

- Introduces a threshold not found in approved evidence.
- Changes a source-provided threshold.
- Uses incompatible units.
- Labels a value normal, abnormal, high, low, or critical without supporting evidence.
- Converts a numeric trend into a clinical conclusion without explicit support.
- Resolves a conflict that should have been escalated.

## 12. Provenance

Threshold-based statements should retain privacy-safe provenance including:

- Source identifier.
- Source version.
- Citation identifier.
- Threshold or reference-range source category.
- Draft version.
- Validation status.
- Reviewer identifier when applicable.

Telemetry must not retain raw patient data, prompts, retrieved source text, credentials, or raw provider errors.

## 13. Fail-Closed Rule

If the system cannot determine the authoritative threshold, compatible unit, source version, or required approval:

```text
unknown or conflicting threshold
→ do not interpret
```

The value may be displayed when otherwise authorized, but unsupported clinical interpretation must remain withheld.

## 14. Training Limitation

**This is a fictional Clinical Threshold Policy created for a synthetic software demonstration. Any example thresholds are synthetic and are not medical guidance, clinical reference ranges, or standards of care. This document must not be used to interpret results or make decisions for real patients.**
