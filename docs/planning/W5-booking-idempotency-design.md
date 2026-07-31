# Week 5 — Booking Idempotency Design (RIV-175, Stage 2)

Repository snapshot: `main` @ `41e979d` (post-Stage-1 merge), 2026-07-31.
Specification only — no service, schema, migration, or UI code is introduced
by this document. Builds on
`docs/planning/W5-RIV-175-problem-scope.md` (§3.1, the same-intent-retry half
of RIV-175) and `docs/planning/W5-booking-acceptance-criteria.md` (F1–F4,
A1–A3, B1–B2). The database-level invariant that prevents concurrent
*different* keys from double-booking a slot, and the exact transaction/SQL
mechanics that tie the two together, are specified separately in
`docs/planning/W5-booking-database-transaction-design.md` — this document
covers the API contract, the idempotency table, and the touchpoints; that one
covers the partial unique index, the statement-by-statement transaction, and
migration safety. Read both before implementing either.

## 1. `Idempotency-Key` header contract

| Property | Specification |
|---|---|
| Header name | `Idempotency-Key` |
| Format | Opaque string, ASCII printable (0x20–0x7E), 1–255 bytes. The server does not require a specific format (e.g. UUID) beyond this — only length and charset are validated — but clients SHOULD generate a UUIDv4 per booking intent to get uniform, collision-resistant keys for free. |
| Scope | `(actor_id, operation, idempotency_key)` — **not the key alone**. `actor_id` originates from the already-authenticated gateway session (`services/gateway/app.py`'s `require_session`, which resolves to a Redis-backed dict containing `username`/`role` — `services/gateway/security.py:52-59`), never from the request body. `operation` is a fixed literal (e.g. `"book_appointment"`), reserved so a future second idempotent endpoint cannot collide with booking keys. This is what stops a different actor from using a guessed or observed key to replay or inspect someone else's booking (Security considerations, §5) — **but only if `actor_id` actually reaches the transaction that claims the key, which runs in scheduling-service, not the gateway.** §1.5 below specifies exactly how it gets there; this is a genuinely new touchpoint, not something the existing gateway→scheduling-service call already does. |
| Missing header | Request proceeds with **no** idempotency record — see §1.4. Not currently rejected; see §6, B2 for the planned transition to required. |
| Malformed header (empty string, >255 bytes, non-printable bytes) | `422` — treated as invalid input, the same class of error as a `BookingRequest` field failing Pydantic validation today. |

### 1.5 Actor identity: the missing link between authentication and the idempotency table

The idempotency claim (`docs/planning/W5-booking-database-transaction-design.md`
§2, Step 1) runs inside **scheduling-service**, which has no session or
authentication mechanism of its own — it is an internal service the gateway
calls directly, and (per `CLAUDE.md`'s "Known Risks / Debt") there is no
authentication between the gateway and internal domain services today.
`require_session` resolves `actor_id` at the **gateway**, but
`services/gateway/app.py`'s `proxy_book` (lines 205-207) currently forwards only
the JSON `payload` to scheduling-service via `_post(...)` — no actor
identity crosses that boundary today, and neither does the
`Idempotency-Key` header (§6). Specifying the header contract without also
specifying how `actor_id` gets from the gateway's already-authenticated
session into scheduling-service's `idempotency_keys.actor_id` column left no
implementable source of truth for the scope this design depends on — a
future implementer literally could not populate `actor_id` correctly from
this document alone.

**Design:** the gateway forwards a second, new internal-trust header,
`X-Actor-Id`, set to `session["username"]` (the same field
`services/gateway/app.py`'s existing `/me` endpoint already returns to
clients at line 104 — a stable, already-used-elsewhere per-account
identifier, not new PHI-adjacent surface). This is deliberately **not** the
same mechanism as `_correlation_headers()`'s `X-Request-Id`
(`services/gateway/app.py:248-253`), whose own comment explicitly states it
is "never derived from the session" for a different, unrelated purpose
(request tracing) — `X-Actor-Id` is a new, distinct header with a new,
distinct purpose. Scheduling-service treats `X-Actor-Id` as authoritative
because, in this repository's existing trust model, only the gateway is
ever the caller (`services/gateway/app.py`'s `SERVICES` map is the only
place scheduling-service's internal URL is used) — this design does not
change or improve that trust model (`CLAUDE.md`'s "Known Risks / Debt"
broader gap remains exactly as-is), it only relies on it for one already-
authenticated field, the same way the rest of this design already relies on
`require_session` having run before any of this logic starts.

If `X-Actor-Id` is absent (a malformed or bypassed internal call — should
not happen given the trust model above, but must degrade safely rather than
silently misattributing the claim): treat the request as if no
`Idempotency-Key` was supplied either (§1.4) — a missing actor identity
means the idempotency scope cannot be correctly constructed, so this
request gets no replay protection, but the database invariant
(`docs/planning/W5-booking-database-transaction-design.md` §1) still
applies regardless, exactly as for any other unkeyed request.

### 1.1 Same key, same payload (the replay case)

The defining case this design exists for: a client's first request either
never returned a response (timeout, dropped connection, client crash) or the
client is deliberately safe-retrying. The second request, bearing the same
key and — after fingerprinting (§2) — the same canonical payload, MUST
receive the **exact outcome already recorded** for that key: the same HTTP
status code and the same response body, without re-attempting the booking.
This holds whether the original recorded outcome was a success (`201`) or a
genuine slot conflict (`409`) that the *first* attempt lost — see
`docs/planning/W5-booking-database-transaction-design.md` §2 for why a lost
race is itself a stored, replayable outcome, not just a success.

### 1.2 Same key, different payload

The client reused an `Idempotency-Key` value for a request that is not, by
fingerprint, the same booking intent (different `slot_id`, different
`patient_id`, or any other changed `BookingRequest` field). This is a client
bug or key-reuse error, not a legitimate retry. Response: `409`, distinguished
in the response body from a slot conflict (§4) so client-side handling does
not confuse "you reused a key incorrectly" with "someone else got the slot."
No appointment is created or altered.

### 1.3 Concurrent request, same key, still in flight

Two requests carrying the same key arrive close enough together that the
first has not yet committed. Because the idempotency claim and the booking
attempt happen in one transaction (`docs/planning/W5-booking-database-transaction-design.md`
§2), the SECOND request's attempt to claim the same key **blocks** at the
database level until the first transaction resolves — this is a property of
how PostgreSQL enforces a unique constraint against a concurrent, still-open
transaction, not application-level polling. Two sub-cases once unblocked:

- The first transaction **committed** — the second request's own claim
  attempt now fails with a unique-constraint conflict; it re-reads the
  now-committed row and replays its outcome exactly as in §1.1.
- The first transaction **rolled back** (crashed before commit, or hit an
  unrelated transient error) — the second request's claim attempt succeeds
  as if it were the first; it proceeds to attempt the booking itself.

To bound how long a request can block on a slow or stuck peer, the
transaction sets a short `lock_timeout` (`docs/planning/W5-booking-database-transaction-design.md`
§3 recommends a default) — a `55P03` (`lock_not_available`) error from that
timeout is mapped to `409` with a body indicating the identical request is
still being processed and the client should retry shortly. This must remain
meaningfully shorter than the gateway's existing 30-second proxy timeout
(`services/gateway/app.py:258`, `_post`'s `timeout=30`) so scheduling-service
can return a clean `409` before the gateway itself gives up.

### 1.4 No key present

The request is accepted (§6, B1: backward compatible) but gets no replay
protection — a retry with no key looks like a brand-new, independent booking
attempt to this layer. **The database invariant in
`docs/planning/W5-booking-database-transaction-design.md` still applies
regardless** — two unkeyed concurrent requests for the *same slot* still
produce exactly one confirmed appointment (acceptance criterion C4); what the
key adds on top is the ability to safely replay a lost response for the
*same* intent instead of that intent's retry looking like a second,
independent attempt (which, without a key, will correctly fail as a slot
conflict against itself if the first attempt actually succeeded — a
degraded-but-safe outcome, not a duplicate booking).

## 2. Canonical request fingerprint

`request_fingerprint` = SHA-256 hex digest over a canonical JSON
serialization of the validated `BookingRequest` fields:

```
{"location": <str|null>, "patient_id": <int>, "provider": <str|null>,
 "reason": <str|null>, "scheduled_for": <ISO-8601 UTC string|null>,
 "slot_id": <int>}
```

Rules:

- **Fields included:** every field currently on `BookingRequest`
  (`services/scheduling-service/schemas.py:46-52`) — `patient_id`, `slot_id`,
  `provider`, `reason`, `location`, `scheduled_for`. All of them are part of
  what F3 calls "the booking intent"; none is excluded.
- **Canonical ordering:** object keys sorted lexicographically before
  serialization, so two structurally-equal payloads fingerprint identically
  regardless of the order fields arrived in the original JSON body.
- **Null handling:** an omitted optional field and an explicit `null` are
  encoded identically (both as JSON `null`), computed from the
  already-Pydantic-validated model, not the raw request body — avoids two
  requests differing only in whether they bothered to send
  `"provider": null`.
- **Timestamp normalization:** `scheduled_for` is serialized as UTC ISO-8601
  (`datetime.isoformat()` after conversion to UTC), not in whatever timezone
  offset the client happened to send, so equivalent instants fingerprint the
  same.
- **Explicitly excluded from the fingerprint:** every HTTP header other than
  `Idempotency-Key` itself (which is part of the *scope*, not the
  fingerprint), the session/auth token (only the `actor_id` it resolves to
  matters, and that's part of scope, not the fingerprint), and any future
  request-tracing/correlation id.
- **Known, accepted limitation:** the fingerprint is an exact-match over the
  validated field values, not a semantic-equivalence check — a `reason` of
  `"Follow-up"` vs. `"Follow-up "` (trailing space) or `"follow-up"`
  (different case) fingerprints differently and would trigger the §1.2
  same-key-different-payload path even though a human would call them the
  same intent. This is stated as a deliberate, documented tradeoff (simplicity
  and determinism over fuzzy matching), not an oversight — consistent with
  this repository's practice of recording known limitations rather than
  silently guessing a normalization rule with no evidence behind it.

## 3. Idempotency table (PostgreSQL)

Design sketch for the future migration
(`docs/planning/W5-booking-database-transaction-design.md` §6 covers the
actual migration file, numbering, and rollback):

```sql
CREATE TABLE idempotency_keys (
    id                    BIGSERIAL PRIMARY KEY,
    actor_id              TEXT NOT NULL,
    operation             TEXT NOT NULL,
    idempotency_key       TEXT NOT NULL,
    request_fingerprint   TEXT NOT NULL,
    state                 TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress | completed
    appointment_id        INTEGER REFERENCES appointments(id),
    response_status_code  SMALLINT,
    response_body         JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at          TIMESTAMPTZ,
    CONSTRAINT idempotency_keys_scope_key
        UNIQUE (actor_id, operation, idempotency_key)
);
```

Notes on shape:

- `state` only ever has two application-visible values. As
  `docs/planning/W5-booking-database-transaction-design.md` §2 shows, a row
  is only ever readable by a *different* transaction once it has committed —
  and by the time this transaction commits, the outcome (success or a lost
  race) is already known and recorded. `'in_progress'` is therefore a
  transient, single-transaction-lifetime value, never a state another
  request's SELECT actually branches on (see §1.3 — concurrency there is
  handled by blocking, not by reading `'in_progress'`).
- `response_body` stores the **minimum needed to replay** — for a success,
  `{"appointment_id": ..., "status": "confirmed"}`; for a stored slot
  conflict, the same generic conflict body every slot conflict gets (§4, A3
  — never patient-identifying). It does not duplicate the full original
  request payload (`request_fingerprint` already captures request identity)
  and never stores a raw database exception message (Security
  considerations, §5).
- `appointment_id` is nullable because a completed row whose outcome was a
  slot conflict has no appointment to reference.
- No `CHECK` constraint is specified here on `state`'s two values; whether to
  add one is an implementation-time choice, not a design requirement.

### 3.1 Retention

**Recommended default, not yet a confirmed decision** (per
`docs/planning/W5-booking-acceptance-criteria.md` §5's "Idempotency key
retention" open item): retain completed rows for **24 hours** after
`completed_at`, matching common industry practice for this exact pattern.
After expiry, a background process deletes the row; a subsequently reused
key is then treated as an entirely new, unrelated request (§1.4 behavior,
not §1.1 or §1.2 — the prior use is gone, not remembered). This default,
and the delete-vs-archive choice, requires sign-off from whoever owns this
decision before Stage 2 is treated as final — it is proposed here, not
assumed.

## 4. Error mapping

| Case | HTTP status | Notes |
|---|---|---|
| Replay of a successful original booking | `201` | Body identical to the original success response. |
| Replay of an original slot-conflict outcome | `409` | Same generic conflict body as a fresh slot conflict (§ below) — a retry of a request that already lost the race gets told the same thing, not something new. |
| Same key, different payload (§1.2) | `409` | Body distinguishes this from a slot conflict — e.g. a distinct `error` field value (`"idempotency_key_reused"` vs. `"slot_conflict"`); exact schema is an implementation detail, not fixed here. |
| Concurrent identical request still in flight, lock-wait bound exceeded (§1.3) | `409` | Distinct `error` value (e.g. `"request_in_progress"`) from both cases above. |
| Fresh slot conflict — two different keys, or one keyed and one unkeyed request, racing for the same slot | `409` | Never reveals the other patient/actor (`docs/planning/W5-booking-acceptance-criteria.md` A3). This is the database invariant firing, detailed in `docs/planning/W5-booking-database-transaction-design.md` §2. |
| Malformed `Idempotency-Key` header, or any existing `BookingRequest` validation failure | `422` | Unchanged from today's normal Pydantic validation behavior for the latter; new for the former. |
| Genuinely transient failure (database unavailable, connection error, anything not one of the above) | `503` | Unchanged from today's existing broad exception handling in `services/scheduling-service/app.py:109-113` — but that handler must be narrowed so the two `409` cases above are caught and mapped *before* falling through to this generic branch, not swallowed by it. This narrowing is a future implementation touchpoint (§7), not made in Week 5. |

## 5. Security considerations

- **Actor/key scoping is the authorization-adjacent control here** (§1,
  scope table): it prevents key-guessing/replay across actors. It is
  explicitly **not** a substitute for real per-patient booking authorization
  — `docs/planning/W5-RIV-175-problem-scope.md` §3.4 already draws this line;
  this document does not blur it.
- **Idempotency lookup happens after authentication, not before.** `actor_id`
  used for scoping comes from the already-validated session
  (`require_session`); the idempotency table is never queried with an
  unauthenticated actor value, so it cannot become an oracle an unauthenticated
  caller can probe by guessing keys. This property depends on `X-Actor-Id`
  (§1.5) only ever being set by the gateway after `require_session` succeeds
  — scheduling-service does not, and should not, independently re-validate
  it, consistent with this repository's existing (documented, unchanged)
  gateway-trusts-services-and-vice-versa model.
- **No raw request bodies, headers, or database exception text are stored or
  logged.** `request_fingerprint` is a one-way hash; `response_body` stores
  only the minimal replay fields listed in §3, not the original request.
  Matches this repository's existing PHI-safe logging convention
  (`libs/safe_logging`, `docs/planning/phi-safe-logging-policy.md`) even
  though the idempotency table itself is application data, not a log stream —
  the same minimum-necessary principle applies to both.
- **Conflict responses are generic** (§4, A3) — a losing request never learns
  who won the slot.
- Synthetic/seed identifiers only were used anywhere evidence needed an
  example in this document; no real appointment or patient data was
  referenced.

## 6. Future implementation touchpoints (not modified in Week 5)

| Area | File(s) | What will need to change |
|---|---|---|
| Gateway header forwarding | `services/gateway/app.py` (`proxy_book`, `_post`) | `proxy_book` currently takes `payload: dict` with no access to request headers or the resolved `session`; it will need the incoming `Idempotency-Key` header AND a new `X-Actor-Id: session["username"]` header (§1.5), both forwarded via `_post`'s existing (already-present, currently unused for this call) `headers` parameter (`services/gateway/app.py:256`). |
| Scheduling endpoint / schema / booking path | `services/scheduling-service/app.py`, `schemas.py`, `book.py` | `create_appointment` needs to read both headers (FastAPI `Header(...)`), compute the fingerprint, and run the new transaction (`docs/planning/W5-booking-database-transaction-design.md` §2) in place of `book()`'s current check-then-insert. `BookingRequest`/`BookingResponse` need no new body field — both the key and the actor identity travel as headers, per convention (§1, §1.5). |
| Frontend key generation | `frontend/app/appointments/page.tsx` (`book()`) | Must generate one key per booking *intent* (one per "Book" click for a given slot) and **reuse the same key** across any client-side retry of that same intent — never mint a fresh key on retry, or the whole design is defeated at the source. |
| Database schema / migration | `db/schema.sql`, a future `db/migrations/011_*.sql` (next available number after `010_pgvector_embeddings.sql`) | New `idempotency_keys` table (§3) and the partial unique index — full detail in `docs/planning/W5-booking-database-transaction-design.md`. |
| Tests | `tests/` (new booking test module + concurrency harness) | Stage 3 (`docs/planning/W5-booking-test-vectors.md`) turns this design into concrete vectors; no test exists yet (`tests/README.md:26-27`). |
| Runbook | `docs/runbook.md` (existing RIV-175 section) | The current manual reconciliation instructions will need updating once the fix ships — not done in Week 5; flagged so it isn't forgotten. |

Only Stage 3's own test-vector and handoff documents change as a result of
this list; none of the files named above are modified by this Stage 2 spec.
