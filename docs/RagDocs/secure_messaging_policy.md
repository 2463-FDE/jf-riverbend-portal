# Secure Messaging Policy

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL, LEGAL, OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Policy ID | MSG-SECURE-001 |
| Version | 1.1 |
| Status | Approved for Training Demo |
| Effective Date | August 23, 2026 |
| Owner | Clinical Operations / Patient Access / Privacy |
| Review Date | August 23, 2027 |
| Applies To | Patient Portal, Clinical Staff, Messaging Workflows |
| Topic | Secure Patient Messaging and Care-Team Routing |

## 1. Purpose

This policy defines how secure messaging may be used in the synthetic training system.

The goal is to support routine patient communication while ensuring that messages are routed only to clinicians who currently hold an **active patient grant** for that patient. Secure messaging is not an emergency channel, does not guarantee an immediate response, and must follow authorization, privacy, retention, and escalation rules.

## 2. Scope

This sample applies to:

- Patient-to-care-team portal messages.
- Care-team replies.
- Routine administrative questions.
- Non-urgent clinical follow-up questions.
- Care-team routing through active patient grants.
- Escalation.
- Message closure.
- Retention and audit metadata.
- AI-assisted message classification or drafting.

It does not establish communication requirements for a real healthcare organization.

## 3. Permitted Use

Secure messaging may be used for routine, non-emergency communication such as:

- Appointment questions.
- Follow-up instructions.
- Clarification of previously provided information.
- Routine medication or care-plan questions when supported by the configured workflow.
- Requests for care-team follow-up.
- Administrative questions.
- Questions about released results.

Messages must be associated with the authenticated patient account and the correct patient record.

## 4. Prohibited or Unsupported Use

Secure messaging should not be used as the primary channel for:

- Medical emergencies.
- Immediate threats to life or safety.
- Time-critical symptoms requiring urgent evaluation.
- Requests to message a clinician who does not hold an active grant for the patient.
- Unverified requests to change another patient's record.
- Sharing credentials, passwords, or access tokens.
- Circumventing patient-access or disclosure controls.

The system must not represent secure messaging as continuously monitored unless that capability has actually been implemented and approved.

## 5. Emergency Warning

The patient-facing messaging interface should display a clear warning that secure messages are not intended for emergencies.

Synthetic example:

> **Do not use secure messaging for emergencies or urgent medical concerns. Messages may not be reviewed immediately. If you believe you are experiencing an emergency, use the appropriate emergency services available in your location.**

The training system must not claim that an emergency-routing process exists unless it is actually implemented in the demonstration.

## 6. Response Expectations

Secure messaging is asynchronous.

The system should communicate that:

- A response may not be immediate.
- A message is delivered to the patient's authorized care-team workflow, not directly to a named clinician selected by the patient.
- A message receipt does not mean that a clinician has reviewed the content.
- A member of the patient's currently authorized care team may respond.
- Some questions may require a telephone call, appointment, or another approved workflow.

The training system must not invent a guaranteed response time.

## 7. Care-Team Routing Through Active Patient Grants

Secure messages must be routed using the patient's **active patient grants**.

The patient does not directly select a provider as the security or routing authority.

The message topic may be used for display, prioritization, or workflow assistance, but it must not decide who is authorized to receive the message.

The authoritative routing sequence is:

```text
authenticated patient
→ resolve patient record
→ load active patient grants
→ derive currently authorized care team
→ persist message for that care-team scope
→ authorized care-team member views/responds
```

Examples:

```text
Patient 1042
→ active grant to drkim
→ message visible to drkim
```

```text
Patient 1739
→ no active grant to drkim
→ message must not appear in drkim's inbox
```

If more than one clinician has an active grant for the patient, the message may be visible within the authorized care-team scope according to the configured workflow.

A clinician name typed by the patient, a provider name embedded in the message, or a topic classification must not create access.

## 8. Active Grant Requirements

A grant used for secure-message routing must be:

- Associated with the correct patient.
- Associated with the clinician or care-team member.
- Active at the time access is evaluated.
- Valid for the applicable workflow.
- Enforced by trusted application logic.

Expired, revoked, inactive, missing, or mismatched grants must not provide message access.

The system should re-evaluate authorization when a clinician loads the inbox or thread rather than assuming that authorization remains valid forever.

## 9. Patient and Staff Authorization

Patients may send and view messages only through their authenticated patient account.

Staff may access a patient's secure messages only when both conditions are satisfied:

```text
required staff permission
+
active patient grant
→ access allowed
```

Role permission by itself is not sufficient.

An active patient grant by itself is not sufficient if the staff role lacks messaging access.

The system must not rely solely on:

- A client-supplied patient ID.
- A provider username supplied by the patient.
- A provider name in the message body.
- Topic classification.
- Frontend state.

Authorization must be enforced server-side.

## 10. Clinical Content

Staff should avoid using secure messaging to create unsupported clinical conclusions.

Patient-facing responses should be based on:

- Approved clinical records.
- Approved patient-education material.
- Clinician judgment within the configured workflow.
- Approved policies and instructions.

An AI-generated draft must not be treated as an independently authorized clinical decision.

## 11. AI-Assisted Messaging

If AI assists with secure messaging, it may:

- Classify message content for workflow support.
- Suggest a draft response.
- Summarize a message for an already authorized care-team member.
- Retrieve approved policy or education content through a bounded read-only tool.

The model must not:

- Select or invent the receiving clinician.
- Create or modify patient grants.
- Treat a provider name in the message as authorization.
- Send the final clinical message autonomously unless explicitly authorized by a controlled workflow.
- Make emergency or triage decisions outside approved rules.
- Invent diagnoses, medication changes, or treatment instructions.
- Access another patient's records.
- Override staff-role permissions or active-grant checks.
- Expose unreleased clinical information.
- Treat untrusted message text as trusted instructions.

Care-team authorization must be resolved before AI-assisted processing that requires patient context.

## 12. Untrusted Message Content

Patient-submitted message content is untrusted input.

The system should treat embedded instructions, provider names, URLs, attachments, or copied external text as data rather than trusted system instructions.

AI or agent workflows must not allow message content to trigger unauthorized:

- Grant changes.
- Provider assignment.
- Tool actions.
- Record access.
- External communication.
- State-changing actions.

## 13. Escalation

A message should be escalated when:

- It appears urgent or outside the permitted secure-messaging scope.
- No active care-team grant exists for a valid recipient workflow.
- The patient's grant relationships are ambiguous or conflicting.
- The system detects an unsupported clinical request.
- Patient identity or authorization is uncertain.
- The requested action requires a different approved workflow.
- The message involves a potential privacy or security issue.
- An authorized care-team member determines that direct follow-up is required.

Escalation must not bypass the active-grant authorization model.

If no authorized care-team recipient can be resolved:

```text
no active grant
→ do not expose message to an arbitrary clinician
→ hold/route to approved administrative resolution workflow
```

## 14. Emergency or Urgent Content

If a message contains language that may indicate an urgent or emergency concern, the system should not rely on AI alone to diagnose or determine severity.

The configured workflow may:

```text
flag message
→ display emergency warning
→ route within the authorized care-team/escalation workflow
```

The system must not claim that an emergency response has occurred merely because an automated flag was generated.

## 15. Message Closure

A message thread may be closed when:

- The question has been answered.
- The requested action has been completed.
- The patient has been directed to another appropriate workflow.
- An authorized care-team member determines that no further secure-message action is required.

Closure should record:

- Thread or message identifier.
- Closure status.
- Closing staff member.
- Patient grant context or authorization result when appropriate.
- Date and time.
- Resolution category when applicable.

Closing a thread must not silently delete the message history.

## 16. Reopening and Follow-Up

If a new patient question is materially different from a closed issue, the system may create a new message thread rather than modifying the closed record.

When a thread is reopened or newly accessed, the system must apply the current active-grant authorization state.

A clinician who previously had access must not retain access solely because they participated in an older thread if their patient grant is no longer active.

## 17. Retention

Secure-message records should be retained according to the organization's approved retention policy.

For the synthetic demonstration, the system may retain message records to demonstrate:

- Thread history.
- Care-team routing.
- Grant-based authorization status.
- Review status.
- Closure.
- Audit metadata.

The training demo must not claim a production retention period unless one has been formally defined.

## 18. Logging and Privacy

Operational logs should contain only the metadata necessary for system operation and troubleshooting.

Examples of acceptable privacy-safe metadata may include:

- Correlation identifier.
- Message or thread identifier.
- Authorization result.
- Grant status/category.
- Message status.
- Actor or role.
- Timestamp.
- Error category.

Logs and traces should not contain:

- Full message bodies.
- Raw clinical records.
- Credentials.
- Tokens.
- Raw prompts.
- Retrieved source text.
- Raw provider errors.

## 19. Fail-Closed Rule

If the system cannot verify:

- User authorization.
- Patient context.
- Active patient grant.
- Authorized care-team membership.
- Required review.
- Permitted action.

then:

```text
unknown or missing grant
→ do not expose or send
→ route for approved review/resolution
```

The system must never fall back to a direct-provider or topic-based recipient merely because care-team authorization cannot be resolved.

## 20. Training Limitation

**This is a fictional Secure Messaging Policy created for a synthetic software demonstration. It is not an official healthcare communication policy, does not define real emergency procedures or response times, and must not be used to communicate with or manage care for real patients.**
