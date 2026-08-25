# Appointment Scheduling Policy

**SYNTHETIC TRAINING SAMPLE — NOT FOR CLINICAL OR OPERATIONAL USE**

| Field | Value |
|---|---|
| Policy ID | SCHED-001 |
| Version | 1.1 |
| Status | Approved for Training Demo |
| Effective Date | August 24, 2026 |
| Owner | Patient Access / Scheduling Operations |
| Review Date | August 1, 2027 |
| Applies To | Patient Portal, Front Desk, Scheduling Staff |
| Topic | Appointment Scheduling and Changes |

## 1. Purpose

This policy defines the rules for creating, changing, cancelling, and confirming appointments in the synthetic Community Health Network training portal.

The goal is to support reliable scheduling while preventing duplicate bookings, unauthorized changes, and inconsistent appointment records.

## 2. Scope

This sample applies to appointment workflows involving:

- New appointment requests.
- Appointment confirmation.
- Rescheduling.
- Cancellation.
- Patient portal appointment access.
- Staff-assisted scheduling.
- Duplicate-booking prevention.

It does not define real clinical scheduling standards or emergency-care procedures.

## 3. Appointment Creation

An appointment may be created only when:

- The patient record is identified and authorized.
- The requested provider or service is valid.
- The requested date and time are available.
- Required scheduling information is present.
- The booking operation does not create a duplicate or conflicting reservation.

The system must return a clear success or failure result for every scheduling request.

## 4. Duplicate Booking Prevention

The system must prevent the same appointment request from creating multiple reservations.

Where supported, scheduling requests should use an **idempotency key** or equivalent request identifier.

Example:

```text
Patient: 1737
Provider: Dr. Smith
Date: 2026-09-10
Time: 10:30 AM
Idempotency Key: sched-1737-20260910-1030
```

If the same valid request is submitted again with the same idempotency key, the system should return the existing appointment rather than create another one.

Database-level uniqueness or transactional controls should also protect against simultaneous duplicate requests.

## 5. Availability and Conflicts

Before confirming an appointment, the system must verify that the requested slot is still available.

A request must fail safely if:

- Another appointment already occupies the slot.
- The provider is unavailable.
- The requested scheduling data is incomplete.
- The booking transaction cannot be completed reliably.

The system should not report an appointment as confirmed until the booking transaction succeeds.

## 6. Rescheduling

A reschedule must be treated as a controlled change to an existing appointment.

The system should:

1. Confirm that the existing appointment belongs to the authorized patient.
2. Verify that the new slot is available.
3. Create or reserve the new slot safely.
4. Update or cancel the previous appointment according to the scheduling workflow.
5. Record the resulting appointment status.

If the reschedule fails, the existing appointment should remain unchanged unless the system can prove the replacement was completed successfully.

## 7. Cancellation

An appointment may be cancelled only by an authorized patient or staff member.

The system should record:

- Appointment identifier.
- Patient identifier.
- Cancellation status.
- Date and time of cancellation.
- Actor or workflow responsible for the cancellation.

A cancelled appointment must not appear as an active appointment.

## 8. Patient and Staff Authorization

Patients may access or modify only appointments associated with their own authorized record.

Staff scheduling actions must follow assigned role permissions.

The system must not rely on a user-supplied patient identifier alone to authorize an appointment action.

Authorization should be derived or verified by trusted application logic.

The scheduler and front-desk roles may retrieve scheduling workflow material needed to find and book slots. Scheduling access does not grant `records.read` and must not expose clinical notes, laboratory interpretation, or other clinical-education categories. A legacy or unrecognized role must not expand retrieval scope by asking the model to select a different audience.

## 9. Confirmation and Display

A confirmed appointment should display, when available:

- Date.
- Time.
- Provider or service.
- Location.
- Appointment status.
- Relevant scheduling instructions.

The portal must not display an appointment as confirmed when the backend booking operation is incomplete or failed.

## 10. Fail-Closed Behavior

If the system cannot confirm authorization, slot availability, booking success, or appointment ownership, it must not complete the scheduling action.

Examples:

```text
Unknown authorization
→ Do not book

Conflicting slot
→ Do not book

Database transaction failure
→ Do not report success

Duplicate retry
→ Return existing booking when safely identifiable
```

## 11. Audit and Traceability

The system should retain sufficient metadata to reconstruct a scheduling decision, including:

- Appointment identifier.
- Patient identifier.
- Request or correlation identifier.
- Action performed.
- Appointment status.
- Actor or role.
- Timestamp.
- Idempotency key when applicable.

Logs should avoid unnecessary patient data and must not contain credentials or secrets.

## 12. AI and Agentic Scheduling

If an AI or agent assists with scheduling:

- The model may interpret a request or propose an appointment action.
- The model must not independently authorize access.
- Availability must be checked through trusted scheduling logic.
- Booking, cancellation, and rescheduling must be executed through bounded tools.
- Tool arguments must be validated before execution.
- Duplicate-booking and transaction controls remain enforced outside the model.
- The system must fail closed when required information or authorization is missing.

The model proposes the action; trusted application code decides whether the action is allowed and performs it.

## 13. Exceptions and Escalation

Requests that cannot be safely completed should be routed to staff review.

Examples include:

- Ambiguous patient identity.
- Conflicting appointment records.
- Unsupported scheduling requests.
- Repeated booking failures.
- Missing provider or service configuration.

Staff should not bypass authorization or duplicate-booking controls to force a booking.

## 14. Training Limitation

**This is a fictional appointment scheduling policy created for a synthetic software demonstration. It is not an official healthcare scheduling policy and must not be used to operate a real clinic, schedule real patients, or make clinical decisions.**
