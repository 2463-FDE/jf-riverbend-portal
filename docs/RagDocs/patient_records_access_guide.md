# Patient Records Access Guide

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Document ID | GUIDE-REC-ACCESS-001 |
| Version | 1.1 |
| Status | Approved for Training Demo |
| Effective Date | August 24, 2026 |
| Owner | Health Information Management / Security |
| Review Date | August 1, 2027 |
| Applies To | Patients, Front Desk, Clinical Staff, Administrative Staff |
| Topic | Patient Record Access and Authorization |

## 1. Purpose

This guide defines how the synthetic training system controls access to patient records.

The goal is to ensure that users see only the information their identity, role, and patient relationship authorize them to access.

## 2. Core Rule

Access to patient information must be determined by trusted application logic.

The system must not grant access based only on:

- A patient identifier supplied in a URL.
- A patient identifier typed by the user.
- A model-generated decision.
- A role name provided by the client.
- A hidden frontend field.

Authorization must be verified on the server.

## 3. Patient Access

A patient may access only records linked to their own authenticated portal account.

Example:

```text
patient-1737
→ patient record 1737
→ allowed
```

```text
patient-1737
→ patient record 1042
→ denied
```

The patient must not be able to change a URL or request parameter to access another patient's chart.

## 4. Staff Access

Staff access is role-based and should follow the approved permission matrix.

Examples may include:

### Front Desk

May access approved registration and scheduling information required for patient service.

May not access clinical notes, laboratory detail, clinician review queues, or restricted clinical summaries unless explicitly granted.

### Clinical Staff

May access patient clinical information required for care and assigned workflow responsibilities.

### Administrative / IT Staff

May manage accounts or system configuration according to assigned permissions.

Administrative access does not automatically grant clinical chart access.

### Explicit Role Boundaries

- Front desk may register, capture insurance and consent, and schedule, but must not retrieve clinical notes or laboratory interpretation.
- Laboratory staff may post results under their write permission but do not gain prior-chart read access.
- Billing staff may work with coverage and payment data but must not retrieve clinical notes or clinical-education categories.
- ROI clerks use a bounded document list for disclosure fulfilment and do not read note bodies through the education corpus.
- IT administrators manage accounts and audit configuration but have no patient-scoped retrieval.
- Management uses oversight and reporting access, not chart convenience. Individual chart access requires an appropriately assigned clinical role.

These boundaries describe permitted policy retrieval; they never replace endpoint permission checks or patient-level grants.

## 5. Patient-Level Authorization

Role permission alone is not enough.

A user must also be authorized for the specific patient or workflow.

Example:

```text
role permission
+
patient/workflow authorization
→ access allowed
```

If either condition fails:

```text
→ access denied
```

## 6. Server-Derived Identity

Sensitive actions should use identity derived from the authenticated session or trusted server context.

The application should not trust a client-supplied value such as:

```text
patient_id=1737
```

without verifying that the authenticated user is allowed to act on that patient's record.

## 7. Access to Laboratory Results

Laboratory results may be displayed only when:

- The requester is authorized for the patient.
- The result is in a releasable state.
- Applicable release rules have been satisfied.
- The system can identify the authoritative result record.

Patient-facing AI summaries do not create additional access rights.

If a patient cannot access the source result, the AI summary must not expose it.

## 8. Access to AI-Generated Summaries

A patient may see only the exact AI-summary version that has been approved for patient release.

The patient must not see:

```text
pending drafts
rejected drafts
unreviewed drafts
clinician-only notes
validation failures
```

Clinician review queues require separate staff permissions.

A user's ability to view their own approved summary does not grant access to the review workflow.

## 9. Front Desk Boundaries

Front Desk users may support registration, identity confirmation, invitation workflows, and scheduling according to approved policy.

They must not gain clinical access merely because they can locate a patient.

Example:

```text
Find patient for registration
≠
Read clinical chart
```

The system must enforce that distinction in backend authorization.

## 10. Service-to-Service Access

Internal services must authenticate to one another using approved service credentials or equivalent trusted mechanisms.

A request originating from another container or internal network location must not automatically be trusted.

Internal service authorization should be enforced independently of end-user permissions.

## 11. AI and Agentic Access

If an AI or agent retrieves patient information:

- The model must not decide whether the requester is authorized.
- Authorization must occur before retrieval.
- Retrieval must be bounded to the authorized patient and approved source categories.
- The model should receive only the minimum data required for the task.
- Tool access should be read-only unless the workflow explicitly requires a controlled write.
- Sensitive actions must remain behind trusted application logic.

The safe order is:

```text
authenticate
→ authorize
→ scope patient
→ retrieve approved data
→ model/agent processing
```

Not:

```text
retrieve everything
→ ask model what user should see
```

## 12. Deny-by-Default Behavior

If the system cannot determine a valid role, patient relationship, permission, or authorization state:

```text
unknown
→ deny
```

Unmapped or inactive accounts must not fall back to a broad default role.

## 13. Audit and Traceability

The system should retain privacy-safe access metadata including:

- User or service identifier.
- Role.
- Patient identifier when appropriate.
- Requested action.
- Authorization result.
- Timestamp.
- Correlation identifier.

Logs should avoid unnecessary patient content and must not contain credentials, raw prompts, retrieved clinical text, or secrets.

## 14. Failed Access Attempts

Unauthorized requests should:

- Return an appropriate denial response.
- Avoid exposing unnecessary information about the patient or record.
- Be recorded according to the audit policy.
- Not reveal whether a protected record exists when doing so would create an information leak.

## 15. Exceptions and Escalation

If staff believe additional access is required, access must be resolved through the approved role or authorization process.

Users must not bypass controls by:

- Sharing accounts.
- Reusing another user's session.
- Changing patient identifiers manually.
- Calling an internal service directly.
- Asking an AI agent to retrieve information outside the authorized scope.

## 16. Training Limitation

**This is a fictional Patient Records Access Guide created for a synthetic software demonstration. It is not an official healthcare access-control policy and must not be used to authorize access to real patient records or operate a real clinical system.**
