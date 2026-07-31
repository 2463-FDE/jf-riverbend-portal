# Week 5 — RIV-175 Problem Scope: "Calendar Glitch" Is Two Backend Correctness Problems

Repository snapshot: `main` @ `fe55e3a`, 2026-07-31. Documentation only — no
service, schema, migration, or UI code is introduced by this document. Line
numbers below were re-checked against current `main` at the time of writing
and should be re-checked again before Stage 2 implements against them.

## 1. Purpose

RIV-175 was reported by Billing as a "calendar glitch":

> A couple patients say they got two appointment confirmations for one
> booking, and two people showed up for the same slot once.
> (`docs/handover/jira-tickets.md`, RIV-175)

"Calendar glitch" points an implementer at the frontend calendar/slot display.
The actual defect is entirely in the backend: `services/scheduling-service`
performs an unguarded check-then-insert with no request-retry identity and no
database constraint preventing two confirmed rows for one slot. This document
separates the single symptom report into the two distinct correctness
problems it actually describes, cites the exact evidence for each, and states
what the fix must observably do — without proposing or writing any code.

## 2. Exact current-state evidence

### 2.1 The race: check-then-insert with no transaction, no constraint

`services/scheduling-service/book.py:23-77`:

```python
def slot_taken(slot_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM appointments WHERE slot_id = %s AND status = 'confirmed'",
        (slot_id,),
    )
    taken = cur.fetchone() is not None
    conn.close()
    return taken


def insert_appointment(...) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO appointments (...) VALUES (...) RETURNING id", (...))
    aid = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return aid


def book(...):
    """
    Classic check-then-act race. Two near-simultaneous requests (or a client
    retry of a slow POST) both pass slot_taken() and both insert. There is no
    UNIQUE constraint on slot_id and no idempotency key on the request, so the
    same slot ends up double-booked.
    """
    if not slot_taken(slot_id):
        time.sleep(0.05)
        return insert_appointment(...)
    return None
```

Each of `slot_taken()` and `insert_appointment()` opens and closes its **own**
connection — there is no shared transaction spanning the check and the insert,
and the module's own docstring (`book.py:1`) states the race is "load-bearing
brownfield debt." The `time.sleep(0.05)` between the check and the insert is
not a bug introduced by this document's analysis; it is present in the
current code and widens an already-real race window.

`services/scheduling-service/app.py:93-125` (`create_appointment`) calls
`book()` directly, with no idempotency key anywhere in the request path, and
maps a `None` return (slot already taken by the time of insert, or — under
the race — simply lost the check) to `BookingResponse(status="slot_taken")`.
A caller cannot distinguish "the slot was genuinely already booked when I
asked" from "my request was silently dropped by the race" — both produce the
same response.

### 2.2 No idempotency identity anywhere in the request path

- `services/scheduling-service/schemas.py:46-57` — `BookingRequest` carries
  `patient_id`, `slot_id`, and optional visit fields only. No key, token, or
  nonce field exists.
- `services/gateway/app.py:205-207` (`proxy_book`) takes `payload: dict` and
  the caller's `session`, and forwards only the JSON body via `_post(...)`
  (`services/gateway/app.py`, `_post` helper) — it does not read or forward
  any request header, so even if a client sent an `Idempotency-Key` header
  today, the gateway would silently drop it before it reached
  scheduling-service. (`_post()`'s own signature already accepts an optional
  `headers` dict — see `docs/planning/W5-booking-idempotency-design.md`,
  Stage 2 — but `proxy_book` does not pass one through.)
- `frontend/app/appointments/page.tsx:55-78` (`book()`) POSTs
  `{patient_id, slot_id, provider, reason}` with no generated key, and its
  only error handling is a generic "Could not book that slot" message
  (line 74) — a client-side retry after a timeout or a dropped response
  would resubmit the identical payload with no way for the backend to
  recognize it as the same intent.

### 2.3 No database invariant

`db/schema.sql:68-87`:

```sql
CREATE TABLE IF NOT EXISTS appointments (
    id            SERIAL PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    slot_id       INTEGER NOT NULL,            -- NOTE: no UNIQUE constraint, no FK
    ...
    status        TEXT NOT NULL DEFAULT 'confirmed',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
```

`appointments.slot_id` has neither a foreign key to `slots.id` nor any
uniqueness protection. `slots.status` (`db/schema.sql:71`, comment: `open |
booked (advisory only)`) is not read or written by the booking path at all —
`book.py` and `create_appointment` never touch `slots.status`; the only
"is this slot taken" check is the `appointments` table scan in
`slot_taken()`. There is no database-level mechanism of any kind — trigger,
constraint, or otherwise — that can reject a second confirmed appointment for
the same slot. `db/migrations/006_providers_and_slots.sql`'s own comment
(line 4) states this was known at the time slots were introduced: "NOTE:
appointments.slot_id still has no UNIQUE constraint and no FK to slots."

### 2.4 Confirmed as a real production incident, not a hypothetical

- `docs/handover/jira-tickets.md`, RIV-175 (Billing-reported, quoted above).
- `docs/runbook.md`, "Two confirmations / two people for one slot (RIV-175)"
  — the current operational mitigation is a **manual SQL query** to find
  duplicate confirmed rows and cancel the later one by hand:
  ```sql
  SELECT slot_id, count(*) FROM appointments
  WHERE status='confirmed' GROUP BY slot_id HAVING count(*) > 1;
  ```
  This is reconciliation after the fact, not prevention.
- `ARCHITECTURE.md:99` lists "Double-booking (RIV-175)" as a named, current
  system risk, attributing it to the same check-then-insert race with no
  UNIQUE constraint.
- `docs/analysis/system-audit-07-18-2026.md` (AUD-14) confirms the defect is
  **unchanged** as of the most recent audit pass and explicitly states "W5's
  idempotency spec should include this fix as a requirement" (line 145) and
  that the spec must cover "both the client-side retry safety AND the
  database-level prevention of duplicate booking under concurrency — they are
  separate problems" (`docs/analysis/system-audit-plan-07-18-2026.md`,
  line 27) — the same two-problem framing this document adopts independently
  from the code evidence above.

### 2.5 No test coverage of the race

`tests/README.md:26-27`:

> **No tests for the scheduling race / double-booking** (`book.py`). The
> happy path is exercised manually only.

This is stated as a deliberate, documented coverage gap, not an oversight —
consistent with `CLAUDE.md`'s framing of this repository's debt as
intentional and tracked, not silently missing.

## 3. What RIV-175 actually is: two distinct problems

The single symptom report ("two confirmations," "two people for one slot")
describes two mechanically different failure modes that a fix must address
separately. Conflating them is exactly how "calendar glitch" became the
working name for a backend race condition.

### 3.1 Same-intent retry (the "two confirmations" half)

One patient, one booking intent, submitted more than once — a slow request
retried by the browser, a double-click, a lost response that the client
never saw. Today: `frontend/app/appointments/page.tsx`'s `book()` has no
concept of "this is the same attempt as before" — a retry is
indistinguishable from a second, independent booking request, and nothing
downstream can tell the two apart either (§2.2 above). **Fix shape:** an
idempotency key scoped to one request intent, so a retry replays the
original outcome instead of attempting a second booking.

### 3.2 Concurrent claims on one slot (the "two people for one slot" half)

Two different actors, two different intents, both targeting the same slot at
close to the same time. An idempotency key does not solve this — two
different, legitimate requests each get their own key, and each looks
individually valid. Today: nothing at any layer — application, gateway, or
database — can reject the second one once the first has already read
`slot_taken() == false` (§2.1, §2.3 above). **Fix shape:** a database-level
invariant (per the "Recommended implementation" in
`.claude/skills/w5-deliverable-planner/SKILL.md`, a partial unique index on
confirmed appointments per slot) that makes a second confirmed row for the
same slot physically impossible to commit, regardless of how many concurrent
requests reach the insert.

These two problems require two different mechanisms and neither substitutes
for the other: an idempotency key with no database constraint still allows
two different keys to double-book a slot; a database constraint with no
idempotency key still fails a legitimate retry-after-timeout by turning it
into a `409` indistinguishable from an actual conflict.

### 3.3 Cancellation followed by a later, valid rebooking

`services/scheduling-service/app.py:128-140` (`cancel_appointment`) sets
`appt.status = "cancelled"` on an existing row. A later, independent booking
request for the *same slot*, after a legitimate cancellation, must be allowed
to succeed — it is not a duplicate of the cancelled appointment and must not
be rejected by whatever mechanism prevents §3.2. This is a real interaction
between the fix and existing behavior, not a hypothetical edge case, and is
scoped **in** for Stage 2's design (which appointment `status` values the
partial unique index's `WHERE` clause must and must not cover) and Stage 3's
test vectors, but no cancellation/rebooking *policy* decision (e.g., a cool-
down period, or whether the original patient must be the one to rebook) is
made here — see §5.

### 3.4 Authorization — related, explicitly out of scope for this fix

`services/gateway/app.py`'s `require_session` (line 57) validates that a
session exists; it does not bind the session's actor to the `patient_id` in
the booking payload, consistent with this repository's single flat `staff`
role (`config/roles.yaml`) and the documented lack of per-action
authorization (`CLAUDE.md`, "Known Risks / Debt"). **This is a real gap, but
it is a different defect from RIV-175** — idempotency and a slot-uniqueness
constraint prevent duplicate/racing *bookings*, not an unauthorized booking
*for the wrong patient*. Fixing RIV-175 must not be described as also fixing
who is allowed to book for whom; that is Week 4's authorization scope, not
this one.

## 4. What this document does not decide

The following are identified as real, open questions in §5 of
`docs/planning/W5-booking-acceptance-criteria.md`, not resolved here:
deliberate/administrative overbooking, exact cancellation-to-rebooking
timing rules, idempotency key retention duration, and the exact replay
response/status code for a client that never received the original response.

## 5. Scope boundary for Week 5

**In scope (specification only, per
`.claude/skills/w5-deliverable-planner/SKILL.md`):** defining the idempotency
contract, the database invariant, the atomic transaction shape, migration
safety for existing duplicate rows, and test vectors that make §3.1 and §3.2
independently verifiable.

**Out of scope:** any code, test, schema migration, configuration, or UI
change; patient-to-actor authorization (§3.4); observability/audit tooling
beyond what Week 5's design must not preclude (see
`docs/planning/W5-booking-idempotency-design.md`, Security considerations).
