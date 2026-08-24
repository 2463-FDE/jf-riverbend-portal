# Intake and Consent Guide

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL, LEGAL, OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Document ID | GUIDE-INTAKE-CONSENT-001 |
| Version | 1.0 |
| Status | Approved for Training Demo |
| Effective Date | August 1, 2026 |
| Owner | Patient Access / Privacy / Clinical Operations |
| Review Date | August 1, 2027 |
| Applies To | Patient Intake, Registration, Portal Activation, Consent Workflows |
| Topic | Intake Data Collection and Consent Handling |

## 1. Purpose

This guide defines how the synthetic training system collects patient intake information and records consent-related decisions.

The goal is to collect only the information needed for the workflow, preserve patient authorization boundaries, and ensure that consent values are recorded clearly and are not invented, inferred, or changed by an AI model.

## 2. Scope

This sample applies to:

- New patient intake.
- Demographic information.
- Contact information.
- Insurance information.
- Optional sensitive identifiers.
- Consent acknowledgements.
- Portal activation.
- Staff review of intake information.
- AI-assisted explanations of intake questions.

It does not establish requirements for real patient registration, consent, treatment, or legal authorization.

## 3. Core Rule

Patient information must be collected for a defined purpose and handled according to trusted application rules.

The system must not treat missing information as consent.

Example:

```text
consent = yes
→ record yes

consent = no
→ record no

consent = unanswered
→ record unanswered
```

The system must not convert:

```text
unanswered
→ yes
```

## 4. Intake Data

The synthetic intake workflow may collect fields such as:

- First name.
- Last name.
- Date of birth.
- Address.
- City.
- State.
- ZIP code.
- Phone number.
- Email address.
- Insurance information.
- Optional SSN or other identifier when configured for the demonstration.
- Intake notes.
- Consent values.

Only information required for the intended workflow should be requested.

## 5. Required and Optional Fields

The user interface should distinguish clearly between:

```text
required
```

and:

```text
optional
```

A patient should not be forced to provide an optional field merely because the backend data model supports it.

If a required value is missing, the system should return a clear validation message rather than invent a value.

## 6. Patient Identity

Intake information may help identify or match a patient record, but it must not automatically establish identity when ambiguity exists.

Example:

```text
exact match
→ may enter configured confirmation workflow

partial or conflicting match
→ staff review
```

The system must not merge patient records solely because an AI model believes two records refer to the same person.

## 7. Duplicate and Possible-Match Handling

When intake data resembles an existing patient record:

- Exact-match logic should follow approved deterministic rules.
- Partial matches should be surfaced for staff confirmation.
- Ambiguous matches should not be auto-merged.
- Staff confirmation should be recorded when the workflow requires it.
- Failed or uncertain matching should fail safely.

Example:

```text
possible duplicate
→ review

not:
possible duplicate
→ automatic merge
```

## 8. Consent Presentation

Consent language should be presented in clear, understandable terms.

The system should identify:

- What the patient is being asked to agree to.
- Whether the consent is required or optional.
- What action records the choice.
- How the choice is stored.
- Whether the patient may later withdraw or change the choice within the configured workflow.

The synthetic system must not represent a checkbox as legal consent outside the training scenario.

## 9. Consent Recording

A consent record should retain sufficient metadata to reconstruct the decision.

Examples include:

- Patient identifier.
- Consent type.
- Consent value.
- Version of the consent text.
- Date and time.
- Workflow or actor recording the decision.
- Correlation identifier when applicable.

The exact consent value must come from the patient or authorized workflow, not from model inference.

## 10. AI-Assisted Intake Explanations

If AI assists the intake workflow, it may:

- Explain an intake question in simpler language.
- Select among approved explanation text.
- Help classify a non-sensitive administrative request when explicitly permitted.

The model must not:

- Invent consent.
- Change a patient's answer.
- Fill missing demographic fields.
- Guess SSN, insurance, or identity information.
- Decide that two patient records should be merged.
- Make clinical diagnoses from intake content.
- Receive unnecessary patient data when an explanation can be provided without it.

Where possible, patient-facing intake explanations should use approved, hard-coded or otherwise governed content.

## 11. Minimum Necessary Data for AI

If an AI model is used, send only the information required for that specific task.

Example:

```text
Task:
Explain what "insurance member ID" means

Needed:
approved explanation context

Not needed:
name
DOB
SSN
address
clinical notes
```

Do not send the full intake request to a model simply because the application already has it in memory.

## 12. Logging

Runtime logs must not record full intake request bodies.

Logs should use an allowlist of safe operational metadata such as:

- Correlation identifier.
- Request outcome.
- Workflow stage.
- Error category.

Logs must not contain:

- Name.
- DOB.
- SSN.
- Address.
- Phone number.
- Insurance identifiers.
- Consent content.
- Raw patient notes.
- Credentials or secrets.

## 13. Portal Activation and Access

Completing an intake form does not automatically grant portal access to a patient record.

Portal activation should use a separate controlled process.

The safe order is:

```text
intake
→ identity/record workflow
→ invitation or activation
→ authenticated account
→ patient-level authorization
```

The system must not treat knowledge of a patient identifier as proof of authorization.

## 14. Staff Review

Staff review may be required when:

- Patient matching is ambiguous.
- Required information is inconsistent.
- A consent value is unclear.
- A possible duplicate exists.
- A workflow exception occurs.

Where staff confirmation is required, the system should record who confirmed the decision and when.

## 15. Corrections

If intake information is corrected:

- Preserve the authoritative updated value.
- Record changes according to the configured audit approach.
- Do not silently rewrite consent history.
- Do not allow an AI-generated suggestion to become the stored value without the required human or patient action.

## 16. Fail-Closed Rule

If the system cannot determine:

- Patient identity.
- Required intake values.
- Consent state.
- Authorization.
- Match status.
- Required staff confirmation.

then:

```text
unknown
→ do not assume
→ do not merge
→ do not grant access
→ route for review
```

## 17. Training Limitation

**This is a fictional Intake and Consent Guide created for a synthetic software demonstration. It is not an official healthcare registration or consent policy, is not legal advice, and must not be used to collect consent or operate a real patient intake process.**
