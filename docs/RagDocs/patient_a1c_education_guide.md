# Patient A1c Education Guide

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL USE**

| Field | Value |
|---|---|
| Document ID | EDU-A1C-001 |
| Version | 1.1 |
| Status | Approved for Training Demo |
| Effective Date | August 24, 2026 |
| Owner | Patient Education / Clinical Governance |
| Review Date | August 1, 2027 |
| Audience | Adult Patients |
| Topic | Hemoglobin A1c (HbA1c) |

## 1. Purpose

This guide explains, in simple language, what an A1c test measures and how patients may see A1c results reported in the portal.

It is intended for a synthetic training demonstration only. It does not diagnose diabetes, recommend treatment, or replace advice from a licensed healthcare professional.

## 2. What Is A1c?

A1c, also called **hemoglobin A1c** or **HbA1c**, is a blood test that reflects average blood glucose levels over the previous several months.

The result is usually reported as a **percentage (%)**.

Example:

```text
A1c: 6.2%
```

## 3. Why Is A1c Measured?

A healthcare professional may use A1c along with other clinical information to:

- Monitor blood glucose trends over time.
- Support diabetes screening or management.
- Compare a current result with earlier results.
- Discuss whether additional follow-up may be appropriate.

An A1c result should not be interpreted by itself without the rest of the clinical context.

## 4. Understanding the Number

The portal may show:

- Current A1c value.
- Unit (`%`).
- Date of the result.
- Laboratory reference information, when available.
- Previous A1c values for comparison.

A patient-facing summary may repeat these source facts.

It may also perform simple arithmetic when the values use the same analyte and unit.

### Synthetic Example

```text
Previous A1c: 7.5%
Current A1c:  6.2%

Difference: 7.5 - 6.2 = 1.3 percentage points
```

A safe summary could say:

> The recorded A1c changed from 7.5% to 6.2%, a decrease of 1.3 percentage points.

The system should not add terms such as **normal**, **abnormal**, **good**, **bad**, **controlled**, **improving**, or **worsening** unless an approved source explicitly supports that interpretation.

## 5. What A1c Does Not Tell You

An A1c result does not, by itself:

- Explain why blood glucose changed.
- Identify a specific cause.
- Provide a diagnosis.
- Determine which medicine or treatment is appropriate.
- Replace a clinician's assessment.
- Describe short-term changes that may not be reflected in the average.

## 6. General Test Ranges and Limitations

For this synthetic training guide, approved federal patient education describes the following general diagnostic ranges for the test:

- Below 5.7%: no diabetes on this test.
- 5.7% to 6.4%: prediabetes range.
- 6.5% or above: diabetes range.

These ranges explain the test; they are not an individualized target and must not be used by the portal to diagnose the signed-in patient. When a person has no symptoms, federal education says that a result in a diagnostic range should be confirmed with a repeat test on another day before a diagnosis is treated as established.

Point-of-care A1c samples analyzed in an office should not be used for diagnosis. Conditions affecting red blood cells—including some hemoglobin variants, anemia, and kidney failure—can make A1c results misleading. A clinician may need a different glucose test or additional context.

## 7. When to Ask Your Care Team

Patients may want to ask their care team:

- What does this result mean for me?
- How does it compare with my previous results?
- Are there other test results that should be considered with this one?
- Do I need another test or follow-up visit?
- Are there any changes to my care plan?

The portal should direct clinical questions to the care team rather than inventing an answer.

## 8. AI-Generated Explanations

If an AI-generated explanation is used in the training portal:

- It must use only approved source information.
- It must preserve the exact reported value, unit, and date.
- It must not invent diagnoses, causes, treatment recommendations, or clinical significance.
- Unsupported statements must be refused or withheld.
- The exact generated draft must be reviewed by a clinician before patient release.
- Pending, rejected, or superseded drafts must not be shown to the patient.
- The approved version displayed to the patient must be the same version the clinician reviewed.

## 9. Source and Provenance Expectations

A displayed A1c explanation should be traceable to its source data.

At minimum, the system should retain:

- Source result identifier.
- Result date.
- A1c value and unit.
- Draft version, if AI-generated.
- Citation/source identifiers.
- Review status.
- Reviewer identifier, when applicable.

Logs and traces should not contain raw patient data, prompts, retrieved text, credentials, or raw provider errors.

## 10. Training Limitation

**This is a fictional patient-education guide created for a synthetic software demonstration. It is not medical advice, is not an official hospital policy, and must not be used to diagnose, treat, or make decisions about a real patient.**
