# Week 5 — Booking Fix Acceptance Criteria (RIV-175)

Repository snapshot: `main` @ `fe55e3a`, 2026-07-31. Specification only — see
`docs/planning/W5-RIV-175-problem-scope.md` for the problem this criteria set
is written against. Every criterion below must be observable (a specific
HTTP status/body, a specific database state, or a specific log/metric
absence) and traceable to a future test vector in
`docs/planning/W5-booking-test-vectors.md` (Stage 3). No criterion here is
satisfied by this document — nothing has been implemented or tested yet.

## 1. Functional

| # | Criterion | Observable outcome |
|---|---|---|
| F1 | A single booking request with a valid idempotency key succeeds exactly once. | Exactly one `appointments` row with `status='confirmed'` for that slot; `201` with `status="confirmed"` and an `appointment_id`. |
| F2 | A retried request with the same key and the same payload replays the original outcome, not a new booking attempt. | No second `appointments` row is created; the second HTTP response is byte-identical (or explicitly documented as re-derivable, per Stage 2) to the first. |
| F3 | A retried request with the same key but a **different** payload (different `slot_id`, `patient_id`, or other booking field) is rejected as a key-scope violation, not silently applied. | `409` with a body distinguishing "key reused with a different request" from "slot conflict" (exact code/body defined in Stage 2). No booking side effect occurs. |
| F4 | A request with no idempotency key is still accepted (backward compatibility during client migration, per the plan's "Recommended implementation"). | Behaves as today for the happy path, but is still subject to F5/F6 below — an unkeyed request gets no replay convenience but the database invariant still applies. |
| F5 | A cancelled appointment's slot can be legitimately rebooked by a later, independent request. | `cancel_appointment`'s existing transition (`status → 'cancelled'`) is not treated as "slot still confirmed" by the uniqueness mechanism; the later booking succeeds and produces its own confirmed row. |
| F6 | Deliberate/administrative overbooking (if such a decision is later made) is not silently permitted or silently blocked by this fix without an explicit design decision. | Not testable yet — recorded as an open business decision in §5, blocking on an explicit answer before Stage 2 finalizes the partial-index `WHERE` clause. |

## 2. Concurrency

| # | Criterion | Observable outcome |
|---|---|---|
| C1 | Two concurrent requests with the **same** idempotency key for the same slot result in exactly one confirmed appointment and one consistent replayed response for both callers. | One `appointments` row; both HTTP responses report the same `appointment_id` and `status="confirmed"` (or one succeeds and the other is told to retry the in-progress key — exact behavior for the "still in progress" state is defined in Stage 2, not guessed here). |
| C2 | Two concurrent requests with **different** idempotency keys (different actors/intents) racing for the same slot result in exactly one confirmed appointment; the other is rejected. | One `appointments` row with `status='confirmed'` for the slot; the losing request receives a stable `409`, never a second confirmed row, never a `500`/`503` for what is actually a normal conflict. |
| C3 | Two concurrent requests for **different** slots never interfere with each other. | Both succeed independently; no serialization or lock contention crosses slot boundaries (rules out a fix that takes a table-wide lock as its concurrency control). |
| C4 | Two concurrent requests **without** any idempotency key, racing for the same slot, still result in exactly one confirmed appointment. | Same database outcome as C2 — the database invariant (not the idempotency key) is what actually prevents the double-booking; the key only adds replay convenience on top. This is the criterion that most directly falsifies a fix that relies on the idempotency key alone. |

## 3. API / contract

| # | Criterion | Observable outcome |
|---|---|---|
| A1 | The `Idempotency-Key` header's format, length limit, and scope (actor + operation) are explicit. | Defined in `docs/planning/W5-booking-idempotency-design.md`; a key outside the allowed shape is rejected with `422`, not silently accepted or silently truncated. |
| A2 | Error responses distinguish key-misuse (F3), slot conflict (C2), invalid input, and transient database unavailability. | Four distinct, stable outcomes — `409` (key misuse), `409` (slot conflict), `422` (invalid input), `503` (transient DB failure) — never collapsed into one generic error, and never a bare `500`. |
| A3 | A slot-conflict response never reveals which other patient or actor holds the slot. | Response body for C2/F3 contains no `patient_id`, name, or other identifying detail belonging to the request that "won." |

## 4. Migration / data safety

| # | Criterion | Observable outcome |
|---|---|---|
| M1 | Existing duplicate confirmed rows for the same slot (if any exist in a running deployment) are detected **before** the partial unique index is created. | A named preflight query (the same shape as `docs/runbook.md`'s existing manual reconciliation query) runs and reports duplicates; the migration does not attempt to create the index against data that would violate it. |
| M2 | The migration is additive and reversible. | A rollback path exists that does not require reversing already-applied application code changes; matches this repository's existing migration style (`db/migrations/00N_*.sql`). |
| M3 | The migration does not require an extended exclusive lock that would take booking offline for a materially long window. | Documented lock/online-migration considerations in Stage 2's transaction design; no criterion here claims a specific duration without evidence. |

## 5. Unresolved business decisions (recorded, not guessed)

Each of the following materially changes cancellation, overbooking, or key-
retention behavior and must be answered by a named decision-maker before
Stage 2's design is treated as final, per this document's instruction not to
silently assume an answer:

- **Deliberate/administrative overbooking.** Does any legitimate workflow
  (e.g., a front-desk override) need to intentionally place two appointments
  on one slot? If yes, the partial unique index's `WHERE` clause and the
  conflict-mapping rules in Stage 2 must accommodate an explicit override
  path; if no, this is stated as permanently disallowed.
- **Cancellation-to-rebooking timing.** Is a slot immediately rebookable the
  instant an appointment is cancelled, or is there a cooldown/hold window?
  (§3.3 of the problem scope.)
- **Idempotency key retention.** How long is a completed key's replay record
  kept before it can be reused for a genuinely new, unrelated request? What
  happens if a client reuses an expired key — treated as brand new, or
  rejected?
- **Replay response code on lost-response retry.** When a client's original
  response was never received (network drop, client crash) and it retries
  with the same key, is the replayed response's HTTP status the same as the
  original (`201`) or a distinct "here is what already happened" status
  (e.g., `200`)? This affects client-side handling and must be decided, not
  inferred from convention.
- **Who may book for which patient.** Explicitly out of scope for this fix
  (§3.4 of the problem scope) but flagged here because a future reader of
  these acceptance criteria could mistake F1–F6/C1–C4 as also covering it.
  They do not.

## 6. Observability / operational

| # | Criterion | Observable outcome |
|---|---|---|
| O1 | Key-outcome and conflict events are observable without logging booking payloads or patient identifiers. | Any future logging/metric emits outcome category (e.g., `replayed`, `key_conflict`, `slot_conflict`) and coarse counts only — no `patient_id`, `slot_id`, key value, or raw exception text (mirrors this repository's existing PHI-safe logging convention, `libs/safe_logging`). |
| O2 | The manual reconciliation query in `docs/runbook.md` (RIV-175 section) remains valid or is explicitly superseded by an updated runbook entry once the fix ships. | Not a Week 5 deliverable to update the runbook itself, but Stage 2/3 must not describe a design that makes the existing query wrong without saying so. |

## 7. Backward compatibility

| # | Criterion | Observable outcome |
|---|---|---|
| B1 | A client that has not yet adopted the `Idempotency-Key` header continues to be able to book (F4). | No breaking change to the existing `BookingRequest` schema's required fields. |
| B2 | The header is documented as required **after** a stated client-migration/monitoring period, not optional forever (per the plan's "Recommended implementation"). | Stage 2's design states this transition explicitly rather than leaving the header permissively optional with no end date. |
