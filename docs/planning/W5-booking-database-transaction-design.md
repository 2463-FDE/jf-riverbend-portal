# Week 5 — Booking Database Invariant and Transaction Design (RIV-175, Stage 2)

Repository snapshot: `main` @ `41e979d` (post-Stage-1 merge), 2026-07-31.
Specification only — no migration, schema, or service code is introduced by
this document; every SQL statement below is a design sketch for a future
migration, not something applied now. Companion to
`docs/planning/W5-booking-idempotency-design.md`, which covers the API
contract, the idempotency table, and the fingerprint; this document covers
the invariant that stops two *different* keys (or no keys at all) from
double-booking a slot, and the exact transaction mechanics that make both
halves of the fix atomic together. Addresses
`docs/planning/W5-RIV-175-problem-scope.md` §3.2 (concurrent claims on one
slot) and acceptance criteria C1–C4, M1–M3
(`docs/planning/W5-booking-acceptance-criteria.md`).

## 1. The invariant: a partial unique index, not `SERIALIZABLE`

```sql
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS appointments_one_confirmed_per_slot
    ON appointments (slot_id)
    WHERE status = 'confirmed';
```

This is valid PostgreSQL 15 DDL (this repository's Postgres version, per
`CLAUDE.md`) — the earlier, untracked Week 5 planner's proposal of
`ALTER TABLE ... ADD UNIQUE ... WHERE ...` is **not** valid syntax; a
`UNIQUE` table constraint cannot carry a `WHERE` clause. A partial index
created with `CREATE UNIQUE INDEX ... WHERE ...` is the correct, and only,
way to express "unique among rows matching this predicate" in PostgreSQL.
`CONCURRENTLY` avoids taking a long-lived exclusive lock on `appointments`
while the index builds (§5).

**Why `status = 'confirmed'` and not some other predicate:** the current
seed data (`db/seed/generate_seed.py`, `db/seed/seed.sql`) uses three
`appointments.status` values in practice — `confirmed`, `cancelled`, and
`completed` — and the plan's own recommendation already specifies this
predicate. Restricting the index to `'confirmed'` rows is what makes
`docs/planning/W5-RIV-175-problem-scope.md` §3.3 (cancel, then legitimately
rebook the same slot) work: once a row transitions to `'cancelled'`, it no
longer participates in the uniqueness check, so a later confirmed booking
for that slot_id is not blocked by it.

**Why a partial unique index instead of `SERIALIZABLE` isolation or
`SELECT ... FOR UPDATE` alone**, per the plan's explicit guidance:

- Making the whole service (or transaction) `SERIALIZABLE` forces PostgreSQL
  to detect conflicts via predicate-lock tracking and abort/retry
  transactions non-deterministically under contention — correct, but far
  more invasive than the problem requires, and it changes behavior for every
  other query the service runs, not just booking.
- `SELECT ... FOR UPDATE` locks *existing* matching rows. It cannot prevent a
  *phantom* concurrent insert — there is nothing to lock before the first row
  for a slot exists, so two transactions can each run
  `SELECT ... WHERE slot_id = X FOR UPDATE`, see zero rows, and both proceed
  to insert. A unique index is enforced against inserts directly, regardless
  of what any `SELECT` observed beforehand — it does not depend on there
  being a pre-existing row to lock.
- The default isolation level, `READ COMMITTED`, is sufficient here (§3) —
  correctness comes from the unique index's own locking behavior, not from
  snapshot isolation, so there is no need to elevate isolation level at all.

## 2. The transaction (keyed request)

One PostgreSQL transaction, using a `SAVEPOINT` to isolate the booking
attempt from the idempotency claim so a lost race doesn't undo the claim
itself:

```
BEGIN;
SET LOCAL lock_timeout = '5s';   -- bound how long a request can block; see §3

-- Step 1: claim the idempotency key. Fails immediately (after possibly
-- blocking — see §3) if this (actor_id, operation, idempotency_key) is
-- already claimed by another transaction.
INSERT INTO idempotency_keys
    (actor_id, operation, idempotency_key, request_fingerprint, state)
VALUES
    (:actor_id, 'book_appointment', :key, :fingerprint, 'in_progress')
RETURNING id;
-- -> idempotency_id

SAVEPOINT before_booking;

-- Step 2: attempt the booking itself.
INSERT INTO appointments
    (patient_id, slot_id, provider, reason, location, scheduled_for, status)
VALUES
    (:patient_id, :slot_id, :provider, :reason, :location, :scheduled_for, 'confirmed')
RETURNING id;
-- -> appointment_id, on success

-- Step 3a (success path): record the outcome, then commit.
UPDATE idempotency_keys
SET state = 'completed',
    appointment_id = :appointment_id,
    response_status_code = 201,
    response_body = :success_body,
    completed_at = clock_timestamp()
WHERE id = :idempotency_id;

COMMIT;
```

If Step 2 raises a unique-violation on
`appointments_one_confirmed_per_slot` (someone else's confirmed row for the
same `slot_id` already committed):

```
-- Step 2 failed: roll back only the appointment insert attempt, not the
-- idempotency claim from Step 1.
ROLLBACK TO SAVEPOINT before_booking;

-- Step 3b (conflict path): record the LOST-RACE outcome against the SAME
-- idempotency row claimed in Step 1, then commit. A retry with this same
-- key later replays this 409, not a fresh attempt.
UPDATE idempotency_keys
SET state = 'completed',
    response_status_code = 409,
    response_body = :slot_conflict_body,
    completed_at = clock_timestamp()
WHERE id = :idempotency_id;

COMMIT;
```

If Step 1 itself raises a unique-violation on
`idempotency_keys_scope_key` (another transaction already claimed this
exact key — necessarily already committed by the time this transaction can
see the conflict, since an uncommitted claim blocks rather than conflicts,
per §3):

```
ROLLBACK;  -- nothing else was attempted yet; nothing else to preserve
-- Outside the aborted transaction: SELECT the existing committed row by
-- (actor_id, operation, idempotency_key) and branch per
-- docs/planning/W5-booking-idempotency-design.md §1.1/§1.2 on whether its
-- request_fingerprint matches this request's.
```

**Why a `SAVEPOINT`, not two separate transactions:** the plan requires "one
atomic transaction" that claims the key, attempts the booking, and stores
the outcome. Without a savepoint, a unique-violation on Step 2 would mark
the *entire* transaction as aborted (PostgreSQL's normal behavior after any
statement error), forcing a full `ROLLBACK` that would also discard the
Step 1 idempotency claim — losing the ability to record and later replay the
conflict outcome. The `SAVEPOINT` confines the failure to just the booking
attempt, letting the same transaction continue on to record the outcome and
commit as a single unit.

## 3. Concurrency behavior and isolation level

`READ COMMITTED` (PostgreSQL's default; nothing in this design requires
changing it) is sufficient because unique-index enforcement is not subject to
ordinary MVCC snapshot-visibility rules — PostgreSQL always checks a unique
index against rows from other sessions' **uncommitted** transactions too, by
blocking the checking statement until the other transaction resolves, then
re-checking:

- **Same key, concurrent (§1.3 of the idempotency design):** transaction B's
  Step 1 `INSERT` blocks if transaction A's Step 1 `INSERT` for the identical
  `(actor_id, operation, idempotency_key)` is still open. If A commits, B's
  insert then fails with a unique-violation on
  `idempotency_keys_scope_key`, and B proceeds down the replay/reject path
  above. If A rolls back (crashed, or hit an unrelated error before Step 1's
  effects committed), B's insert succeeds as though B were first. The
  `SET LOCAL lock_timeout = '5s'` bounds this wait; on expiry PostgreSQL
  raises `55P03` (`lock_not_available`), mapped to the `409`
  "request still in progress" outcome
  (`docs/planning/W5-booking-idempotency-design.md` §1.3). 5 seconds is a
  proposed default, chosen to be comfortably shorter than the gateway's
  existing 30-second proxy timeout (`services/gateway/app.py:258`) so
  scheduling-service can return a clean `409` before the gateway's own call
  times out; the exact value is an implementation-time tuning choice, not
  fixed by this document.
- **Different keys (or no keys), same slot (§3.2 of the problem scope,
  criteria C2/C4):** whichever transaction's Step 2 `INSERT ... appointments`
  commits first wins outright. The second transaction's own Step 2 blocks
  (same `lock_timeout` bound) against the first's still-open insert; once
  unblocked, if the first committed, the second gets a unique-violation on
  `appointments_one_confirmed_per_slot` specifically — distinguishable from
  an `idempotency_keys_scope_key` violation by inspecting the exception's
  constraint name (`psycopg2`: `exc.diag.constraint_name`), which is how the
  application tells "key misuse" apart from "slot conflict" (both are
  SQLSTATE `23505`; only the constraint name differs). This is what makes
  C4 (no keys at all, still exactly one confirmed row) hold: the mechanism
  that prevents the double-booking here is the index, not the idempotency
  table, which an unkeyed request never touches.
- **Different slots, concurrent (C3):** no shared index entry, no blocking,
  fully independent.

## 4. Optional: `slot_id` foreign key and its own preflight

Adding `FOREIGN KEY (slot_id) REFERENCES slots(id)` to `appointments` (today
absent — `db/schema.sql:79`, `-- NOTE: no UNIQUE constraint, no FK`) is a
genuinely separate, lower-priority improvement from the double-booking fix
itself: it would guard against an invalid/nonexistent `slot_id` ever being
inserted, but it does nothing to prevent two *valid* confirmed rows on the
*same* `slot_id` — that is the partial unique index's job alone. Recommended
as a nice-to-have in the same future migration **only if its own preflight
passes**: `appointments.slot_id` has been a plain `INTEGER NOT NULL` with no
FK since the very first migration (`db/migrations/001_init.sql:37`), while
`slots` was introduced later (`db/migrations/006_providers_and_slots.sql`) —
so it is not guaranteed every existing `appointments.slot_id` value actually
exists in `slots.id` today. Preflight:

```sql
SELECT a.slot_id, count(*)
FROM appointments a
LEFT JOIN slots s ON s.id = a.slot_id
WHERE s.id IS NULL
GROUP BY a.slot_id;
```

If this returns any rows, adding the FK in the same migration as the unique
index is not safe without a separate decision about those orphaned rows —
treat the FK as independently deferrable, not blocking the double-booking
fix itself.

## 5. Preflight for existing duplicate confirmed rows — real evidence, not hypothetical

Per `docs/runbook.md`'s existing RIV-175 manual mitigation query:

```sql
SELECT slot_id, count(*) FROM appointments
WHERE status='confirmed' GROUP BY slot_id HAVING count(*) > 1;
```

Running the equivalent check directly against this repository's own committed
`db/seed/seed.sql` (parsed, not executed against a live database, for this
Stage 2 evidence pass) finds **35 distinct `slot_id` values with more than
one `'confirmed'` row today**, out of 209 total appointment rows — not just
the one deliberately-authored fixture
(`db/seed/generate_seed.py`'s documented "Two confirmed appointments for the
SAME slot 88231 ~400ms apart (retry race)" — slot `88231` is indeed one of
the 35). The other 34 are incidental: `generate_seed.py`'s randomized
appointment rows (`s_id = random.randint(88200, slot_id-1)`) sample slot ids
without checking for a prior confirmed booking, so collisions accumulate by
chance across the ~7-per-patient sampling loop. **This means a real
migration attempt against seed-loaded data would fail outright** — PostgreSQL
refuses to create a unique index over rows that already violate it — with
35 conflicts to resolve, not 1. The migration must not assume the deliberate
fixture is the only duplicate; it must run the preflight query for real and
branch on however many rows it actually finds, in whatever environment it
runs against (seed-loaded, or an as-yet-uninspected production database).

**Reconciliation process — recommended, not yet a confirmed decision** (this
is exactly the kind of decision
`docs/planning/W5-booking-acceptance-criteria.md` §5 requires naming an
owner for, not guessing): for each duplicate `slot_id`, keep the row with
the earliest `created_at` as `'confirmed'` and set every later row's
`status` to `'cancelled'` — mirroring what the runbook already tells an
operator to do by hand today ("Resolve manually (cancel the later row) until
the booking path is fixed," `docs/runbook.md`), just automated and applied
once, immediately before the index is created, inside the same migration
transaction as a data-fixup step preceding the `CREATE UNIQUE INDEX`. This
needs a named decision-maker's sign-off before Stage 2 is treated as final —
proposed here as the most defensible default given the existing runbook
convention, not asserted as decided.

## 6. Migration mechanics

- **Numbering:** the next available migration is `011_*.sql` — `010` is
  already used by `db/migrations/010_pgvector_embeddings.sql` (merged
  earlier, Week 8 persistence work).
- **Structure (design sketch, not applied in Week 5):**
  1. Preflight query (§5); if it returns rows, run the reconciliation update
     (§5) before proceeding.
  2. `CREATE TABLE idempotency_keys (...)` (full DDL in
     `docs/planning/W5-booking-idempotency-design.md` §3).
  3. `CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
     appointments_one_confirmed_per_slot ON appointments (slot_id) WHERE
     status = 'confirmed';` (§1).
- **Lock/online-migration considerations:** `CONCURRENTLY` avoids a
  long-lived exclusive lock on `appointments` while the index builds, at the
  cost of two full table scans and not being usable inside the same
  transaction block as other DDL — it must run as its own statement, outside
  an explicit `BEGIN`/`COMMIT` wrapping the rest of the migration (this
  repository's migration runner behavior for a `CONCURRENTLY` statement
  specifically is an implementation detail to confirm at build time, not
  assumed here). `CREATE TABLE idempotency_keys` is a fast, low-risk DDL
  statement with no contention concern (new, empty table).
- **Rollback:** additive only — `DROP INDEX IF EXISTS
  appointments_one_confirmed_per_slot;` and `DROP TABLE IF EXISTS
  idempotency_keys;` fully reverse this migration with no data loss to any
  *other* table. The reconciliation step (§5) is **not** trivially
  reversible — cancelling duplicate rows is a real state change; a rollback
  plan must either accept that or design the reconciliation step to be
  restorable (e.g. recording which rows it touched before flipping their
  status), which is an implementation-time decision this document flags but
  does not resolve.
- **Schema/migration synchronization:** this repository keeps
  `db/schema.sql` as a flattened, always-current reflection of every applied
  migration (`CLAUDE.md`'s structure section; confirmed by `schema.sql`
  already containing the Week 8 `rag_embeddings`/pgvector table introduced by
  migration `010`). The future `011_*.sql` migration must be accompanied by
  a matching manual edit to `db/schema.sql` adding `idempotency_keys` and the
  partial index, in the same commit — not deferred, and not left to drift.

## 7. Definition-of-done cross-check

- Same key/same payload deterministically replays one result — §2, Step 3a
  and the replay branch after a §2 idempotency-key conflict.
- Same key/different payload is rejected — the fingerprint comparison in the
  replay branch; full contract in
  `docs/planning/W5-booking-idempotency-design.md` §1.2.
- Different keys racing for one slot yield exactly one confirmed
  appointment — §1, §3 (`appointments_one_confirmed_per_slot` enforcement,
  independent of the idempotency table).
- A missing key loses replay convenience but not the database invariant —
  §3, "different keys (or no keys), same slot."
- Existing duplicates are detected before the unique index is created —
  §5, with real counts from this repository's own seed data, not assumed.
- Transaction and crash-recovery behavior is explicit — §2's SAVEPOINT
  design; §3's isolation-level reasoning for why `READ COMMITTED` (no
  elevated isolation, no `SERIALIZABLE`) is sufficient.
