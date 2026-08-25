# Laboratory Result Release Policy

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL USE**

| Field | Value |
|---|---|
| Policy ID | LAB-REL-001 |
| Version | 1.2 |
| Status | Approved for Training Demo |
| Effective Date | August 24, 2026 |
| Owner | Clinical Governance |
| Review Date | August 1, 2027 |
| Applies To | Patient Portal and Clinical Staff |
| Data Type | Laboratory Results |

## 1. Purpose

This policy defines when laboratory results may be released to the patient portal and when clinician review is required first. It is designed to support timely patient access while preserving review and escalation for results that may require immediate clinical attention.

## 2. Scope

This sample applies to laboratory results received by the Community Health Network training portal, including routine chemistry, hematology, and other structured laboratory observations used in the synthetic demonstration.

## 3. Policy

- Routine laboratory results may be released to the patient portal after the result is final and associated with the correct patient and encounter.
- Preliminary, corrected, cancelled, or otherwise non-final results must not be displayed as final patient results.
- Results marked critical by the laboratory must be routed for clinician review and documented notification before patient-portal release.
- The portal must display the laboratory value, unit, reference range when available, collection/result date, and source record without adding an unsupported clinical interpretation.
- Automated summaries may quote released results or perform simple deterministic calculations, but must not label a result as normal, abnormal, improving, worsening, or clinically significant unless an approved source explicitly supports that statement.
- A patient-facing AI-generated explanation must remain withheld until the exact draft version has completed required clinician review and is approved for release.
- Pending, rejected, superseded, or unreviewed AI-generated drafts must not be visible to the patient.
- If the system cannot confirm patient identity, authorization, result status, or required approval, release must fail closed.

## 4. Critical and Sensitive Results

Critical-result handling takes precedence over routine portal release. The responsible clinical workflow must record the reviewer or notifier and the result requiring action.

The training system may simulate this routing, but must not represent the simulation as a production clinical notification process.

## 5. Corrections and Supersession

When a laboratory result is corrected, the corrected source record becomes authoritative. Previously released values should remain traceable for audit purposes but must not be presented as the current result.

For AI-generated summaries, an already approved version remains visible until a newer version is separately reviewed and approved.

## 6. Audit and Provenance

The system should retain privacy-safe provenance sufficient to identify:

- Source result
- Version
- Release decision
- Reviewer, when applicable
- Correlation identifier

Logs and traces must not contain raw patient data, prompts, retrieved document text, credentials, or raw provider errors.

## 7. Exceptions and Escalation

If a result cannot be released under this policy, the system must withhold it and route the case to the appropriate staff workflow.

Staff should not bypass release controls by copying unreleased content into another patient-facing channel.

Detailed early-release procedures are clinician-only and are governed by the
Clinician Early-Release Appendix. This patient-readable policy does not expose
that procedure or authorize a patient request for early release.

## 8. Training Limitation

**This is a fictional sample policy created for a synthetic training demonstration. It is not an actual hospital policy, does not establish clinical standards of care, and must not be used to make real patient-care decisions.**
