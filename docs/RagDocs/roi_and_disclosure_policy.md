# ROI and Disclosure Policy

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL, LEGAL, OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Policy ID | ROI-DISC-001 |
| Version | 1.0 |
| Status | Approved for Training Demo |
| Effective Date | August 1, 2026 |
| Owner | Health Information Management / Privacy |
| Review Date | August 1, 2027 |
| Applies To | Release of Information, Patient Records, Disclosure Workflows |
| Topic | Authorization, Minimum Necessary, Delivery, and Disclosure Accounting |

## 1. Purpose

This policy defines how the synthetic training system handles requests to release patient information.

The goal is to ensure that a disclosure occurs only when the requester is authorized, the information released is appropriately scoped, the delivery method is controlled, and the disclosure is recorded.

## 2. Scope

This sample applies to:

- Release-of-information requests.
- Patient-authorized disclosures.
- Staff-assisted disclosures.
- External delivery of patient records.
- Minimum-necessary review.
- Disclosure accounting.
- AI- or agent-assisted ROI workflows.

It does not establish legal requirements for real healthcare disclosures.

## 3. Core Rule

A disclosure must not occur unless the system can establish:

```text
valid request
+
valid authorization or permitted basis
+
authorized requester
+
approved record scope
+
approved delivery destination
→ release
```

If any required element is missing or uncertain:

```text
→ withhold
→ route for review
```

## 4. Valid Authorization

When authorization is required, the system should verify that the authorization identifies:

- The patient or subject of the records.
- The information authorized for release.
- The intended recipient.
- The purpose or requested use when applicable.
- The authorization date.
- Any expiration date or event.
- The person authorizing the release.
- The authorization status.

A request must not be treated as authorized simply because it contains a patient ID or recipient email address.

## 5. Authorization Status

Only an authorization that is valid for the current request may support a disclosure.

Examples:

```text
active authorization
+
matching patient
+
matching recipient
+
matching record scope
→ continue
```

```text
expired / revoked / mismatched / missing
→ do not release
```

The system should not allow an AI model to decide whether an authorization is legally valid.

## 6. Minimum Necessary

When the workflow requires minimum-necessary handling, the system should release only the information needed for the approved purpose.

Example:

```text
Request:
final laboratory results for March 2026

Release:
those authorized laboratory results

Do not automatically include:
full chart
clinical notes
billing history
unrelated encounters
```

The system should not use "send complete chart" as a default when a narrower record set satisfies the request.

## 7. Record Selection

Record selection should be enforced by trusted application logic.

The release process should verify:

- Patient scope.
- Record category.
- Date range.
- Final/releasable status.
- Authorization scope.
- Recipient scope.

The model may help explain or classify a request, but it must not independently broaden the disclosure.

## 8. Delivery

Before delivery, the system should verify:

- Approved recipient.
- Approved destination.
- Approved delivery method.
- Record package matches the authorized scope.
- Required review or confirmation is complete.

Example delivery methods in the synthetic demonstration may include:

- Patient portal.
- Approved secure electronic delivery.
- Staff-controlled export.

The training system must not represent ordinary email or an uncontrolled external destination as inherently secure.

## 9. Delivery Confirmation

A disclosure should not be recorded as successfully delivered until the delivery operation completes successfully.

If delivery fails:

```text
release attempt
→ failed delivery
→ do not mark delivered
→ retain failure status
→ route for follow-up
```

Raw provider or transport errors should not be exposed to patients or written to unsafe logs.

## 10. Disclosure Accounting

The system should retain a disclosure record sufficient to reconstruct what occurred.

At minimum, the record should identify:

- Patient identifier.
- Request or authorization identifier.
- Recipient.
- Record categories released.
- Date range when applicable.
- Delivery method.
- Disclosure date and time.
- Staff member or service responsible.
- Outcome.
- Correlation identifier.

The accounting record should not contain more patient content than is needed to identify the disclosure.

## 11. Audit and Traceability

A privacy-safe trace may record:

```text
request received
→ authorization checked
→ scope determined
→ records selected
→ delivery attempted
→ disclosure recorded
```

Telemetry should not contain:

- Full record contents.
- Raw clinical notes.
- Credentials.
- Access tokens.
- Raw authorization documents.
- Raw provider errors.

## 12. AI and Agentic Assistance

If an AI or agent assists with ROI:

- Authorization must be checked outside the model.
- Patient and recipient scope must be enforced outside the model.
- Retrieval tools must be bounded and read-only unless a controlled delivery action is explicitly authorized.
- The model must not decide that a missing authorization is "probably acceptable."
- Record selection must remain constrained by trusted policy.
- Delivery must require validated arguments.
- Consequential release actions should require deterministic mediation and, where appropriate, human approval.

Safe order:

```text
authenticate
→ authorize
→ scope
→ retrieve
→ review
→ deliver
→ account
```

Unsafe order:

```text
retrieve full chart
→ ask model what to send
→ deliver
```

## 13. Fail-Closed Rule

If the system cannot verify:

- Authorization.
- Patient identity.
- Recipient.
- Record scope.
- Delivery destination.
- Required approval.

then:

```text
unknown
→ no disclosure
```

## 14. Exceptions and Escalation

Requests that are incomplete, conflicting, expired, ambiguous, or outside configured policy should be routed to Health Information Management or the designated review workflow.

Staff should not bypass disclosure controls by:

- Sending records through an unapproved channel.
- Expanding the record package beyond the authorized scope.
- Reusing an authorization for a different recipient or purpose.
- Asking an AI agent to override a failed authorization check.

## 15. Training Limitation

**This is a fictional ROI and Disclosure Policy created for a synthetic software demonstration. It is not legal advice, is not an official healthcare disclosure policy, and must not be used to authorize, release, or account for real patient information.**
