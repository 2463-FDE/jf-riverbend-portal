# Week 5 — Booking Fix Implementation Handoff (RIV-175, Stage 3)

Repository snapshot: `main` @ `224f7df` (post-Stage-2 merge), 2026-07-31.
Specification only. This document sequences the **future** implementation
into small, individually reviewable steps and names what still needs a
decision before that work starts — it does not implement, test, or commit
any of it. **The sequence below begins only after this entire Week 5
specification package (Stages 1–3) has been reviewed and approved; nothing
in it is scheduled or started by this document.**

## 1. What this package is, end to end

| Document | Covers |
|---|---|
| `docs/planning/W5-RIV-175-problem-scope.md` | The actual problem, reframed from "calendar glitch" into two backend correctness issues, with exact code/evidence citations. |
| `docs/planning/W5-booking-acceptance-criteria.md` | Twenty observable criteria (F1–F6, C1–C4, A1–A3, M1–M3, O1–O2, B1–B2) plus five recorded open business decisions. |
| `docs/planning/W5-booking-idempotency-design.md` | The `Idempotency-Key` API contract, request fingerprint, `idempotency_keys` table, error mapping, retention default, future touchpoints. |
| `docs/planning/W5-booking-database-transaction-design.md` | The partial unique index, the one-transaction/`SAVEPOINT` design, concurrency/locking analysis, migration mechanics, and a real duplicate-row count from this repo's own seed data. |
| `docs/planning/W5-booking-test-vectors.md` | Twelve numbered vectors (V1–V12) covering every testable criterion, each with setup/interleaving/expected result/database assertion/unacceptable outcomes. |
| This document | Sequencing, remaining touchpoints, and what still needs a decision. |

## 2. Future implementation sequence (small, independently reviewable steps)

Each step below is scoped to be one manually reviewed commit/PR, matching
this repository's existing stage-by-stage pattern. Steps are ordered so each
one is independently testable against the vectors already assigned to it
before the next step adds more surface area.

1. **Migration `db/migrations/011_*.sql` + matching `db/schema.sql` edit.**
   Preflight query, reconciliation update, `CREATE TABLE idempotency_keys`,
   `CREATE UNIQUE INDEX CONCURRENTLY appointments_one_confirmed_per_slot`
   (`docs/planning/W5-booking-database-transaction-design.md` §1, §5, §6).
   Testable in isolation against V10 and a fixture reproducing the seed
   data's 35 existing duplicates, before any application code changes.
2. **Scheduling-service transaction logic.** Replace `book.py`'s
   check-then-insert with the `SAVEPOINT`-based transaction
   (`docs/planning/W5-booking-database-transaction-design.md` §2); update
   `services/scheduling-service/app.py`'s `create_appointment` to read the
   `Idempotency-Key` header (optional — F4), compute the fingerprint
   (`docs/planning/W5-booking-idempotency-design.md` §2), and narrow the
   existing broad `except Exception → 503`
   (`services/scheduling-service/app.py:109-113`) so the two `409` paths
   (key misuse, slot conflict) are caught and mapped before it, not
   swallowed by it. Testable against V1, V2, V3, V6, V9, V11 with a single
   test-database connection — no concurrency harness needed yet.
3. **Gateway header forwarding.** `services/gateway/app.py`'s `proxy_book`
   reads the incoming `Idempotency-Key` header and passes it to `_post`'s
   existing (already-present, currently unused for this call) `headers`
   parameter (`services/gateway/app.py:256`;
   `docs/planning/W5-booking-idempotency-design.md` §6). Small, mechanical,
   low-risk — no new design decision, just wiring already-specified behavior
   through an existing parameter.
4. **Frontend key generation.** `frontend/app/appointments/page.tsx`'s
   `book()` generates one key per booking intent and reuses it across
   retries of that same intent (`docs/planning/W5-booking-idempotency-design.md`
   §6) — no design decision left open here either; this is the one
   remaining touchpoint with no server-side dependency, so it could
   technically land before or after steps 1–3, but verifying it end-to-end
   needs step 2 done first.
5. **Concurrency test harness + integration tests.** V4, V5, V7 need a
   genuine multi-connection barrier against a live Postgres
   (`docs/planning/W5-booking-test-vectors.md`, "Mapping to future test
   layers") — this is new test infrastructure for this repository, not
   reuse of an existing pattern, and is called out separately from step 2's
   simpler unit/API tests because it is meaningfully harder to build
   reliably.
6. **Fault-injection tests (V8).** The hardest layer to automate reliably
   (killed connections/processes at precise points in the transaction) — may
   reasonably start as a documented manual verification procedure before a
   fully automated harness exists, rather than blocking the rest of the
   sequence on it.
7. **Observability (O1, V12).** Safe-logging assertions for conflict/replay
   paths, in the same style as `tests/test_safe_logging.py`. Depends on
   step 2 existing to have something to instrument.
8. **Runbook update (O2).** Update `docs/runbook.md`'s RIV-175 section once
   the fix has actually shipped — explicitly last, since updating it earlier
   would describe behavior that doesn't exist yet.

Steps 1–4 are the actual fix; steps 5–8 are verification and follow-through.
None of them are started, scheduled, or estimated by this document beyond
this ordering.

## 3. Remaining open decisions (block on these, don't guess past them)

Restated from `docs/planning/W5-booking-acceptance-criteria.md` §5 and
`docs/planning/W5-booking-idempotency-design.md` §3.1, with what each
decision gates:

| Decision | Blocks | Currently proposed default (not confirmed) |
|---|---|---|
| Deliberate/administrative overbooking (F6) | The partial unique index's `WHERE` clause — if an override path is needed, the design in Stage 2 must change to accommodate it before step 1 above is implemented. | None proposed — genuinely unresolved; Stage 2's index as specified assumes no override exists. |
| Cancellation-to-rebooking timing | Whether V9's "immediately rebookable after cancel" assumption is correct, or whether a cooldown/hold window is needed. | Immediate rebooking (no cooldown) — matches current `cancel_appointment` behavior with no timing logic today. |
| Idempotency key retention | The reaper/expiry mechanism in step 1's migration and step 2's application logic. | 24 hours, delete-based expiry (`docs/planning/W5-booking-idempotency-design.md` §3.1). |
| Replay response code on lost-response retry | Whether V2's expected `201` (same as original) is correct, or a distinct status (e.g. `200`) should be used for a replayed-but-originally-successful outcome. | `201` (same as original) — simplest, most literal reading of "replay the original outcome" (`docs/planning/W5-booking-idempotency-design.md` §1.1); a distinct status would need its own justification. |
| Header required-after-migration transition (B2) | When/whether an unkeyed request (F4) stops being accepted. | No date proposed — needs a stated client-migration/monitoring period before this transition is scheduled, per the original plan's guidance; not guessed here. |

## 4. Boundaries this fix does not cross (restated so a future implementer doesn't over-scope)

- **Authorization.** Idempotency-key scoping (`docs/planning/W5-booking-idempotency-design.md`
  §1) prevents a different actor from replaying or inspecting *this
  mechanism's* outcomes — it is not, and must not be described as, a fix for
  who is allowed to book for which patient
  (`docs/planning/W5-RIV-175-problem-scope.md` §3.4). This repository has no
  per-action authorization today (`config/roles.yaml`'s single flat `staff`
  role) to test an authorization boundary against; once one exists, a future
  vector should verify that an actor cannot use a guessed/observed
  `Idempotency-Key` belonging to a different, unrelated actor to learn that
  actor's booking outcome — not written now, because the authorization model
  it depends on doesn't exist yet.
- **The RIV-201 IDOR.** Entirely unrelated defect (cross-patient chart reads,
  `docs/analysis/RIV-201-patient-records-IDOR.md`); this fix does not touch
  it, and the existing `xfail` documenting it must not be flipped as a side
  effect of any step in §2.
- **HL7/allergy data loss, ROI authorization, and every other tracked defect**
  named in `CLAUDE.md`'s "Known Risks / Debt" — none of them are touched by
  this fix.

## 5. Statement of record

**Week 5, across all three stages, did not implement or test the RIV-175
booking fix.** No migration was created or applied. No service, gateway, or
frontend code was changed. No test was written or run against the design in
this package. Everything in `docs/planning/W5-*.md` is a specification for
future work, sequenced and decision-gated above, not evidence that the race
condition is fixed.
