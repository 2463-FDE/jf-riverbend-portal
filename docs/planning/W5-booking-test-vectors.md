# Week 5 — Booking Test Vectors (RIV-175, Stage 3)

Repository snapshot: `main` @ `224f7df` (post-Stage-2 merge), 2026-07-31.
Specification only — no test file, fixture, or application code is
introduced by this document. Turns
`docs/planning/W5-booking-acceptance-criteria.md`'s twenty criteria (F1–F6,
C1–C4, A1–A3, M1–M3, O1–O2, B1–B2) and
`docs/planning/W5-booking-idempotency-design.md` /
`docs/planning/W5-booking-database-transaction-design.md`'s design into
deterministic, reproducible vectors. **No vector below has been run. No test
passes yet. Nothing here is a claim that RIV-175 is fixed.**

Every vector states: setup, interleaving/barrier (where concurrency
matters), the request(s) sent, expected HTTP result(s), database
assertion(s), replay assertion(s) where applicable, and explicitly
unacceptable outcomes (the failure modes this vector exists to catch, not
just the happy path). Patient/slot identifiers below are synthetic,
deterministic placeholders (`P1`, `P2`, `S1`, `S2`, ...) — no real
appointment or patient data.

## Coverage map (every acceptance criterion → vector)

| Criterion | Vector(s) |
|---|---|
| F1 | V1 |
| F2 | V2 |
| F3 | V3 |
| F4 | V6 |
| F5 | V9 |
| F6 | Not testable yet — no vector; blocked on the open decision (see `docs/planning/W5-booking-acceptance-criteria.md` §5 and `docs/planning/W5-booking-implementation-handoff.md` §3). |
| C1 | V4 |
| C2 | V5 |
| C3 | V7 |
| C4 | V6 |
| A1 | V3, V11 (malformed key) |
| A2 | V3, V5, V11, V8 |
| A3 | V5, V3 |
| M1 | V10 |
| M2 | Covered by `docs/planning/W5-booking-implementation-handoff.md` §2 (migration structure), not an app-level test vector. |
| M3 | Covered by `docs/planning/W5-booking-database-transaction-design.md` §6 (`CONCURRENTLY`) — an operational property of the migration statement, not a functional test vector. |
| O1 | V12 |
| O2 | Not a code change in Week 5; tracked as a documentation follow-up in the handoff doc, not a test vector. |
| B1 | V6 |
| B2 | Not testable yet — no vector; the required-after-migration transition (`docs/planning/W5-booking-idempotency-design.md` §1, missing-header row) has no stated date/trigger to test against. |

Three criteria (F6, M2/M3, O2, B2) are intentionally not mapped to an
application-level test vector — each is either an unresolved business
decision, a migration-mechanics property better verified by inspection than
a booking-flow test, or an operational/documentation follow-up. This is
explicit, not an oversight.

## Vectors

### V1 — Single keyed request succeeds

- **Setup:** slot `S1` open (no existing appointment). Actor `A1`, key `K1`.
- **Interleaving:** none — single request.
- **Request:** `POST /appointments` with `Idempotency-Key: K1`, body
  `{patient_id: P1, slot_id: S1, ...}`.
- **Expected HTTP result:** `201`, body `{appointment_id: <id>, status:
  "confirmed"}`.
- **Database assertions:** exactly one `appointments` row with `slot_id=S1,
  status='confirmed'`; exactly one `idempotency_keys` row scoped to
  `(A1, 'book_appointment', K1)` with `state='completed',
  response_status_code=201, appointment_id=<id>`.
- **Unacceptable outcomes:** `500`; any status other than `201`/`confirmed`
  on a genuinely open slot with no conflict; more than one `appointments`
  row created by a single request.

### V2 — Same key, same payload, retried after a lost response (replay)

- **Setup:** V1 has already completed and committed (its `idempotency_keys`
  row is `state='completed'`, `response_status_code=201`). Simulates the
  client never receiving the original `201` (dropped connection).
- **Interleaving:** sequential — the retry happens strictly after V1's
  transaction has committed, not concurrently with it (concurrent same-key
  behavior is V4).
- **Request:** `POST /appointments` with the identical `Idempotency-Key: K1`
  and the identical body as V1.
- **Expected HTTP result:** `201`, body identical to V1's original response
  (`docs/planning/W5-booking-idempotency-design.md` §1.1).
- **Database assertions:** still exactly **one** `appointments` row for
  `S1` — the retry must not attempt a second `INSERT`. The
  `idempotency_keys` row for `K1` is unchanged (same `appointment_id`,
  `completed_at` not advanced).
- **Replay assertion:** the second response's body is byte-identical to the
  first (or, if any field is time-derived and intentionally excluded from
  replay, that exclusion is documented — no such field exists in this
  design's `response_body` shape today).
- **Unacceptable outcomes:** a second `appointments` row; a `409` on a
  request that should replay a `201`; a response body that differs from the
  original (e.g. a different `appointment_id`).

### V3 — Same key, changed payload (key-scope violation)

- **Setup:** V1 has completed for `K1` with `{patient_id: P1, slot_id: S1,
  ...}`.
- **Interleaving:** sequential, after V1 commits.
- **Request:** `POST /appointments` with the same `Idempotency-Key: K1` but
  `slot_id: S2` (or any other changed field) — the fingerprint
  (`docs/planning/W5-booking-idempotency-design.md` §2) will differ from the
  one stored against `K1`.
- **Expected HTTP result:** `409`, body with a distinct `error` value from
  the slot-conflict case (e.g. `"idempotency_key_reused"`), per A2.
- **Database assertions:** no new `appointments` row for `S2` (or whatever
  the changed `slot_id` was); the existing `idempotency_keys` row for `K1`
  is unchanged.
- **Unacceptable outcomes:** the request silently succeeds and books `S2`;
  the response body is indistinguishable from a genuine slot conflict (A2,
  A3); a `500`/`503` instead of a stable `409`.

### V4 — Concurrent requests, same key, same slot

- **Setup:** slot `S1` open. Actor `A1`, key `K1`, identical body on both
  requests.
- **Interleaving:** two requests dispatched to arrive as close together as
  the test harness can manage, with an explicit barrier so both reach their
  respective transactions' Step 1 (`docs/planning/W5-booking-database-transaction-design.md`
  §2) before either is allowed to proceed — i.e. force the race rather than
  relying on incidental timing.
- **Requests:** two identical `POST /appointments`, `Idempotency-Key: K1`.
- **Expected HTTP results:** both `201`, both bodies reporting the same
  `appointment_id` — one request's `INSERT` on `idempotency_keys` commits
  first; the second blocks (bounded by `lock_timeout`,
  `docs/planning/W5-booking-database-transaction-design.md` §3) and then
  replays the first's committed outcome once unblocked.
- **Database assertions:** exactly one `appointments` row for `S1`; exactly
  one `idempotency_keys` row for `(A1, 'book_appointment', K1)`.
- **Unacceptable outcomes:** two `appointments` rows; two different
  `appointment_id` values returned to the two callers; either caller
  receiving a `500` instead of a resolved `201`.
- **Variant (V4b) — the winner's transaction rolls back instead of
  committing** (simulated by forcing an error after Step 1 but before
  commit, e.g. a killed connection): the second request's blocked `INSERT`
  must then succeed as if it were first (`docs/planning/W5-booking-database-transaction-design.md`
  §3, "the first transaction rolled back" branch) — expected result: exactly
  one `201`, from whichever request actually completes, with the other
  request's earlier attempt leaving no trace (no orphaned `idempotency_keys`
  row from the rolled-back attempt).

### V5 — Concurrent requests, different keys/actors, same slot

- **Setup:** slot `S1` open. Actor `A1` with key `K1`; actor `A2` with key
  `K2` — two independent, legitimate booking intents for the same slot.
- **Interleaving:** explicit barrier forcing both requests' Step 2
  (`appointments` insert attempt) to race, as in V4.
- **Requests:** two different `POST /appointments`, different keys,
  different `patient_id`, same `slot_id: S1`.
- **Expected HTTP results:** exactly one `201` (winner); exactly one `409`
  (loser) with a slot-conflict body that names neither `A1`/`P1` nor
  `A2`/`P2` (A3).
- **Database assertions:** exactly one `appointments` row for `S1`,
  `status='confirmed'`; the losing actor's `idempotency_keys` row is
  `state='completed', response_status_code=409` (so a retry with `K2`
  replays the `409`, per V2's replay logic applied to a conflict outcome).
- **Unacceptable outcomes:** two confirmed rows for `S1` (the core RIV-175
  defect this whole design exists to close); a `500`/`503` for the loser
  instead of a stable `409`; the loser's response body revealing that `P1`
  or `A1` won.

### V6 — Concurrent requests, no keys, same slot

- **Setup:** slot `S1` open. Two requests, neither carrying an
  `Idempotency-Key` header at all.
- **Interleaving:** same forced-race barrier as V5, applied with no key on
  either side.
- **Requests:** two `POST /appointments`, no `Idempotency-Key` header,
  different `patient_id`, same `slot_id: S1`.
- **Expected HTTP results:** exactly one `201`, exactly one `409` — same
  outcome shape as V5, but produced with **no** `idempotency_keys` row
  involved at all (F4, C4: the database invariant alone is what prevents
  the double-booking here).
- **Database assertions:** exactly one confirmed `appointments` row for
  `S1`; zero `idempotency_keys` rows created by either request.
- **Unacceptable outcomes:** two confirmed rows (this is the vector that
  most directly falsifies a fix relying on the idempotency key alone — C4);
  a crash instead of a clean `409` for the loser.

### V7 — Concurrent requests, different slots

- **Setup:** slots `S1` and `S2`, both open.
- **Interleaving:** dispatched concurrently, no forced barrier needed (there
  should be nothing to force — the point is the absence of interference).
- **Requests:** `POST /appointments` for `S1` and `POST /appointments` for
  `S2` at the same time, any combination of keyed/unkeyed.
- **Expected HTTP results:** both `201`.
- **Database assertions:** one confirmed row each for `S1` and `S2`; no
  detectable serialization delay attributable to lock contention between
  the two (rules out a fix that takes a table-wide lock, C3).
- **Unacceptable outcomes:** either request failing due to contention with
  the other; a measurable latency spike suggesting cross-slot blocking.

### V8 — Database failure, before and after commit

- **V8a (before commit):** inject a failure (e.g. a killed connection, or a
  forced exception) between Step 1 and Step 2 of the transaction
  (`docs/planning/W5-booking-database-transaction-design.md` §2), before
  `COMMIT`. **Expected:** the entire transaction rolls back — zero
  `appointments` rows, zero `idempotency_keys` rows persisted from this
  attempt (PostgreSQL's own atomicity, not application-level cleanup logic —
  `docs/planning/W5-booking-database-transaction-design.md` §3). A
  subsequent retry with the same key behaves as a fresh first attempt (V1),
  not a replay.
- **V8b (after commit, before the HTTP response reaches the client):**
  inject a failure (e.g. kill the application process, or drop the
  connection) after `COMMIT` succeeds but before the response is written
  back. **Expected:** the `idempotency_keys` row and, if the booking
  succeeded, the `appointments` row are both durably committed. A
  subsequent retry with the same key replays the already-committed outcome
  exactly as in V2 — the client experiences this identically to "the first
  response was lost in transit," because from the server's perspective, it
  was.
- **Unacceptable outcomes for both:** a "half-committed" state — an
  `appointments` row with no matching completed `idempotency_keys` row, or
  vice versa, when a key was supplied. (Both live in the same transaction;
  this should be structurally impossible, but the vector exists to prove it,
  not assume it.) For V8a specifically: any persisted row at all. For V8b
  specifically: a retry that re-attempts the booking instead of replaying.

### V9 — Cancellation followed by policy-approved rebooking

- **Setup:** slot `S1` has one confirmed appointment for `P1` (key `K1`).
- **Steps:** (1) `POST /appointments/{id}/cancel` for that appointment —
  existing behavior, `status` becomes `'cancelled'`
  (`services/scheduling-service/app.py:128-140`, unchanged by this design).
  (2) A later, independent `POST /appointments` for the same `slot_id: S1`,
  a different actor/key (`A2`/`K2`), different `patient_id: P2`.
- **Expected HTTP results:** step 1: `200`, `status="cancelled"`. Step 2:
  `201`, `status="confirmed"` — the cancelled row does not block the new
  booking (F5; the partial index only covers `status='confirmed'` rows,
  `docs/planning/W5-booking-database-transaction-design.md` §1).
- **Database assertions:** after step 2, `appointments` has one row for
  `S1` with `status='cancelled'` (the original) and one row with
  `status='confirmed'` (the new one) — two rows for the same `slot_id` is
  **expected and correct** here, precisely because only one of them is
  `'confirmed'`.
- **Unacceptable outcomes:** step 2 rejected with a `409` slot conflict
  against the cancelled row (would indicate the partial index's `WHERE`
  clause is wrong or missing); step 2 silently modifying the already-
  cancelled row instead of creating a new one.

### V10 — Existing duplicate rows encountered during migration

- **Setup:** a database state with more than one `'confirmed'`
  `appointments` row for the same `slot_id` — not hypothetical; this exact
  condition exists today in this repository's own `db/seed/seed.sql` (35
  such `slot_id` values, per
  `docs/planning/W5-booking-database-transaction-design.md` §5).
- **Steps:** run the preflight query
  (`docs/planning/W5-booking-database-transaction-design.md` §5) against
  this data before attempting `CREATE UNIQUE INDEX
  appointments_one_confirmed_per_slot`.
- **Expected result:** the preflight query returns all 35 (or however many
  exist in the environment under test) conflicting `slot_id` groups; the
  migration does **not** proceed to create the index until the
  reconciliation step (keep earliest `created_at` as `'confirmed'`, cancel
  the rest — same section) has run and the preflight query returns zero
  rows.
- **Database assertion:** after reconciliation, re-running the preflight
  query returns zero rows, and `CREATE UNIQUE INDEX
  appointments_one_confirmed_per_slot` then succeeds without error.
- **Unacceptable outcomes:** attempting to create the index against
  unreconciled data (PostgreSQL will reject it, but the vector exists to
  prove the migration's own preflight-then-fix-then-index *sequencing* is
  correct, not to rely on the database's own rejection as the safety net);
  the reconciliation step silently discarding information about which rows
  it changed (see M2 — reversibility concern,
  `docs/planning/W5-booking-database-transaction-design.md` §6).

### V11 — Malformed `Idempotency-Key`

- **Setup:** slot `S1` open.
- **Requests, each independently:** (a) empty-string header value; (b) a
  header value exceeding 255 bytes; (c) a header value containing a
  non-printable byte (e.g. a raw newline or NUL).
- **Expected HTTP result (all three):** `422` (A1) — never accepted,
  truncated, or silently ignored in favor of proceeding as if no key were
  sent.
- **Database assertions:** zero `appointments` rows, zero `idempotency_keys`
  rows created by any of the three requests.
- **Unacceptable outcomes:** any of the three being accepted as valid; any
  of the three being silently treated as equivalent to "no key" (F4)
  instead of a `422`.

### V12 — Observability does not leak PHI on conflict/replay

- **Setup:** any of V3, V5, or V6's conflict outcomes.
- **Assertion:** whatever log lines or metrics are emitted for the
  conflicting/replaying request contain no `patient_id`, no `slot_id`, no
  raw `Idempotency-Key` value, and no raw database exception text — only a
  coarse outcome category (e.g. `replayed`, `key_conflict`, `slot_conflict`)
  and counts (O1). This mirrors the assertion style already used by this
  repository's existing PHI-safe-logging tests
  (`tests/test_safe_logging.py`) and should be implemented the same way —
  inspecting captured log records for absence of sensitive field names, not
  just checking the HTTP response body.
- **Unacceptable outcomes:** a `patient_id`, key value, or exception message
  appearing in any log line emitted during conflict or replay handling.

## Retention expiry and later key reuse — not yet a testable vector

`docs/planning/W5-booking-idempotency-design.md` §3.1 proposes a 24-hour
retention default but flags it as **not yet a confirmed decision**. A vector
for "key expires, then is reused for an unrelated request" cannot be written
correctly until that decision (and the delete-vs-archive mechanism) is
confirmed — writing one now would mean guessing the mechanism this vector is
supposed to verify. Tracked as a follow-up in
`docs/planning/W5-booking-implementation-handoff.md` §3, not given a vector
number here.

## Unauthorized booking/replay attempts — explicitly out of this fix's scope

Per `docs/planning/W5-RIV-175-problem-scope.md` §3.4, patient-to-actor
authorization is a related but separate defect from RIV-175. No vector here
tests "can actor `A1` book on behalf of `patient_id` they're not authorized
for" — that is Week 4 authorization scope. The one authorization-adjacent
property this design does own — that an idempotency key is scoped to the
actor who created it (`docs/planning/W5-booking-idempotency-design.md` §1,
scope row) so a different, unrelated actor guessing or observing a key
cannot replay or inspect someone else's booking outcome through the
idempotency mechanism itself — is worth a dedicated note for the handoff
(`docs/planning/W5-booking-implementation-handoff.md` §3) as a boundary to
test once real per-action authorization exists, but is not written as a
numbered vector here because this repository has no per-action authorization
today to test it against (`config/roles.yaml`'s single flat `staff` role) —
writing the vector now would require fabricating an authorization model this
fix does not introduce.

## Mapping to future test layers

| Vector(s) | Future test layer |
|---|---|
| V1, V2, V3, V6, V9, V11 | Unit/API tests against `services/scheduling-service` with a real (test) Postgres — sequential, no concurrency harness needed. |
| V4, V5, V7 | Integration tests requiring a genuine concurrency harness (threads/processes + a barrier synchronizing both requests' arrival at the relevant transaction step) against a live Postgres — cannot be simulated with mocks, since the behavior under test is PostgreSQL's own lock/unique-index semantics. |
| V8 | Integration tests with fault injection (killed connections/processes) — the hardest layer to automate reliably; may start as a documented manual procedure before a fully automated harness exists. |
| V10 | Migration test — run against a database seeded from `db/seed/seed.sql` (or an equivalent fixture reproducing known duplicates), not a unit test. |
| V12 | Log-capture assertions, same style as `tests/test_safe_logging.py`. |
