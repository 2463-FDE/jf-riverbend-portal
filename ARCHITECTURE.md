# Architecture — Riverbend Patient Portal

> Internal engineering overview. Written by Helix Digital Partners for the
> handoff. Describes the system as it is, including known rough edges.

## 1. Overview

Riverbend Community Health runs a patient intake + records portal: a Next.js
web app talking to a small fleet of FastAPI services behind a backend-for-
frontend (BFF) gateway, backed by Postgres and Redis. It is deployed as a
Docker Compose stack today; "production" is a single VM per clinic region.

```
Browser ──► Next.js portal (3070) ──► gateway / BFF (8070) ──► domain services ──► Postgres / Redis
```

The portal never calls a domain service directly — everything goes through the
gateway, which owns login + session validation and fans requests out.

## 2. Services

| Service | Port | Owns | Data |
|---------|------|------|------|
| gateway | 8070 | login, sessions, request fan-out | `users` (read), Redis sessions |
| intake-service | 8071 | registration, insurance capture, consent, eligibility trigger | `patients`, `insurance_coverages`, `consents` |
| eligibility-service | 8072 | payer eligibility (X12 270/271 over a clearinghouse REST shim) | none (calls payer) |
| records-service | 8073 | patient + chart read façade | `patients`, `encounters`, `records` |
| scheduling-service | 8074 | slot search, booking, cancel | `providers`, `slots`, `appointments` |
| interop-service | 8075 | inbound HL7 v2 ingest from the hospital feed | none (parses to internal shape) |
| roi-service | 8076 | release-of-information requests + disclosures | `roi_requests`, `disclosures`, `records` (read) |

There is **no shared Python library** yet (see `adr/0001`). Each service repeats
the same module layout by copy-paste:

```
config.py          env-driven settings (DB url, redis url, downstream urls)
db.py              SQLAlchemy engine + SessionLocal (lazy — no connect on import)
models.py          SQLAlchemy ORM models for the tables this service touches
schemas.py         Pydantic v2 request/response models
logging_config.py  logging setup
app.py             FastAPI app + routers
```

## 3. Request lifecycle (example: viewing a chart)

1. Portal calls `GET /api/records?patient_id=1042` (a Next.js route handler).
2. That handler forwards to `gateway GET /patients/1042/records` with the
   caller's `Authorization: Bearer <token>`.
3. The gateway's `require_session` dependency validates the token against Redis.
   **It does not bind the session to the requested patient** (see §7, IDOR).
4. The gateway proxies to `records-service`, which assembles encounters and, per
   encounter, its records, and returns the chart.

## 4. Authentication & sessions

- `users` table holds PBKDF2-SHA256 password hashes (django-style encoding).
- `POST /login` verifies credentials and stores a session in Redis
  (`session:<token>` → username, role). The portal keeps the token in
  `localStorage`.
- All non-public gateway routes require a valid session.
- Every account has the single `staff` role (`config/roles.yaml`). There is no
  per-action authorization beyond "is logged in", and **sessions never expire**
  (no TTL on the Redis key; `auth.yaml SESSION_TIMEOUT: never`).
- **TOTP MFA is implemented** (w8-planner-2, migration 033,
  `services/gateway/mfa_*.py`, `config/mfa.yaml`) but ships **off** — see §7
  and `docs/runbook.md` for what activating it actually requires.

See `adr/0003-authentication-and-sessions.md`.

## 5. Data model

Postgres 15 is the single system of record. Flattened schema:
`db/schema.sql`. Ordered forward migrations: `db/migrations/00N_*.sql`
(hand-rolled; kept in sync with `schema.sql` by hand). Demo data is generated
deterministically by `db/seed/generate_seed.py` → `db/seed/seed.sql`
(~250 patients, ~475 encounters, ~690 records, plus appointments, slots,
insurance, ROI requests, and audit rows).

**Selected PHI fields are application-layer encrypted; nothing is encrypted
at the storage layer, and most PHI is still plaintext.**
Exactly `patients.ssn`/`dob`/`notes` (`libs/phi_crypto`, `adr/0012`) —
encrypted by intake-service on write, decrypted by records-service on
read — and `agent_draft_provenance.generated_text` (`adr/0012` follow-up,
migration 032, encrypted and decrypted entirely within records-service)
are AEAD-encrypted. `ssn_digits` is an HMAC-SHA256 blind index, not a raw
digit copy, since migration 031. Every OTHER PHI-bearing column —
`records.title`/`body`/`reference_range`, `encounters.reason`/`allergies`/
`medications`, `patients.address`/`phone`/`email`,
`insurance_coverages.member_id`/`group_number`, secure-message bodies,
among others — remains plaintext; this is field-by-field coverage, not a
blanket claim. Nothing is encrypted at the storage layer — the deployment
is docker compose with a local `pgdata` volume, and key custody for the
fields that ARE encrypted is environment-variable-based, not KMS-backed
(no KMS/secrets-manager integration exists) — and no hop uses TLS,
including `/login`. This paragraph previously claimed storage-layer
encryption and TLS in transit; both were false, and are still false. See
`adr/0008` for the original recorded risk decision, `adr/0012` for the
field-encryption design (and its own follow-up entry covering the agent
draft text), and `adr/0002` for the original data-and-
compliance discussion.

## 6. External integrations

- **Payer eligibility** — `eligibility-service` calls a clearinghouse REST shim
  (X12 270/271). Today this call is synchronous and has no timeout; intake
  triggers it inline on the request path.
- **Hospital HL7 v2 feed** — `interop-service` ingests ADT/ORU messages and maps
  them to the internal record shape.

## 7. Known limitations / tech debt (carried into the handoff)

These are documented honestly so the next team can prioritize. Several were
still open as of the original handoff; some have since been closed in later
catch-up work (cited inline below) — this list is corrected to match current
code, not the original handoff snapshot. Each remaining item is marked open
here; sequencing lives in the current delivery plan, not in this file.

- **Compliance posture is self-asserted.** Most PHI columns are plaintext
  (`adr/0002`) — `patients.ssn`/`dob`/`notes` and
  `agent_draft_provenance.generated_text` are the exceptions (`adr/0012`);
  see §5 above for the full "what is / is not encrypted" list. ~~"audit" is still mutable request logging, not a tamper-evident
  access trail.~~ **Resolved against the threat model this control targets**
  (w8-planner-2 P3, closes AUD-B01). **Threat model:** a compromised or buggy
  runtime/application role (`riverbend_app`) — the credential every service
  actually connects with. That role cannot `UPDATE`, `DELETE`, `TRUNCATE`,
  disable the append-only triggers, or rewrite the chain: a
  `BEFORE UPDATE`/`DELETE` trigger rejects mutation regardless of caller
  (`db/migrations/026_audit_logs_append_only.sql`), it is no longer
  `audit_logs`'s owner and so cannot `ALTER TABLE ... DISABLE TRIGGER`
  (`db/migrations/028_admin_runtime_role_separation.sql`), and every row is
  linked into a hash chain (`db/migrations/027_audit_logs_hash_chain.sql`)
  that `db/migrations/scripts/verify_audit_chain.py` proves detects content
  modification, mid-chain deletion (even if the surviving rows are relinked
  and rehashed), insertion, reordering, and broken links.
  **Explicitly out of scope, not claimed:** a malicious database owner or
  superuser bypassing 026's trigger directly, or truncating the newest rows
  and stopping there — the chain has no way to know rows should still exist
  past that point, and there is no externally stored checkpoint to compare
  against (tracked as follow-up work: an external or HMAC-signed chain-head
  checkpoint; not implemented in this PR stack). This is a tamper-*evident*
  control, not a tamper-*proof* or complete-deletion-detection one.
- ~~**PHI in application logs** — intake logs full request bodies at INFO.~~
  **Resolved.** `services/intake-service/app.py`'s `_intake_log_summary` now
  logs an allowlist only (`correlation_id`, `created_via`), not the request
  body; see the D1 review history in that file's module docstring.
  `logging_config.py`'s docstring described the old behavior and has been
  corrected in this pass.
- **Duplicate patients (RIV-160) — partially resolved.** `/intake` now runs a
  deterministic (dob, ssn) match-key lookup before creating a patient
  (`services/intake-service/app.py::_find_match_candidates`, `adr/0004`): an
  exact match blocks silent creation with a 409, a partial match flags
  `possible_duplicate_match` for staff review. It does not retroactively merge
  patients created before this fix (the seeded Maria Gonzalez fixture's 3 rows
  stay 3 rows) and there is no staff-confirmation UI yet (backend/API only).
- ~~**Slow registration (RIV-088)** — the inline, no-timeout eligibility call
  blocks `/intake`; a payer outage freezes intake (RIV-141).~~ **Resolved**
  (Week 3 catch-up). `/intake` enqueues a bounded async eligibility job
  against `eligibility-service`'s job queue instead of calling the payer
  inline; see `services/intake-service/app.py::_start_eligibility_check` and
  `adr/0005-eligibility-agent-runtime-and-resilience.md`.
- ~~**Double-booking (RIV-175)** — booking is check-then-insert with no UNIQUE
  constraint on `slot_id` and no idempotency key.~~ **Resolved** (Week 5
  catch-up). Fixed at the database level:
  `db/migrations/013_appointment_idempotency_and_uniqueness.sql` plus
  `services/scheduling-service/book.py`'s single-transaction
  check-and-insert. See `tests/integration/test_scheduling_concurrency.py`.
- ~~**IDOR on chart reads** — sessions aren't bound to the patient; sequential
  integer patient IDs are walkable by any authenticated user.~~ **Resolved**
  (Week 4 catch-up, RIV-201). Chart reads now go through
  `services/records-service/patient_access_gate.py` against
  `db/migrations/014_patient_access_grants.sql`; see
  `tests/integration/test_records_flow.py::test_user_cannot_read_other_patients_chart`.
- **N+1 query pattern** in `get_patient_records` — **Resolved** (W10 Final
  Stage 7 sub-slice 4, DEBT D8): live smoke evidence (a real browser "Load
  records" click against the exact merged revision, not just static code
  inspection) proved the route is still called by the current frontend
  (`frontend/app/records/page.tsx`'s "Load records" action, proxied by
  `frontend/app/api/records/route.ts`), so per the stage's own rule that
  decided batching over deprecation. `services/records-service/app.py`'s
  `get_patient_records` now issues 2 queries total regardless of encounter
  count (encounters, then one `IN (...)`-batched records query), not 1+N.
  **Still open, separately: full-table scans.** Neither `records.encounter_id`
  nor `encounters.patient_id` is indexed (`pg_indexes` on a fresh volume
  shows only the two primary keys) — both the old N+1 queries and the new
  batched query are sequential scans over `records`/`encounters`. Batching
  reduces the scan COUNT from 1+N to 2 but does not add the missing
  patient-scoped indexes this line originally paired with the N+1 fix;
  that migration is separate, not-yet-scheduled work.
- **Brittle HL7 mapping** — only PID/PV1 are mapped; AL1 (allergies) and RXA
  (medications) are silently dropped. Still open.
- **ROI has no authorization enforcement** — disclosures go out with no recorded
  45 CFR 164.508 authorization and no accounting trail. Still open.
- **Gateway-to-service trust is now partial.** `intake-service` and
  `records-service` verify a shared `INTERNAL_SERVICE_TOKEN` (fails closed if
  unset); `eligibility-service`, `scheduling-service`, `interop-service`, and
  `roi-service` have no equivalent check and are still fully trusted blind.
  `docker-compose.yml` still publishes every domain service's port to the
  host, not just the gateway's.
- ~~Sessions never expire~~ **Resolved.** `services/gateway/config.py` now
  enforces both an idle TTL (default 8h, refreshed per request) and an
  absolute lifetime cap (default 24h, checked regardless of activity).
- ~~No MFA — deferred to next cycle by client direction (2026-08-12), a bare
  TOTP prototype parked on `feat/mfa-totp-parked`.~~ **Resolved as a
  complete rollout** (w8-planner-2, PR #101, migration 033,
  `services/gateway/mfa_*.py`, `config/mfa.yaml`): TOTP enrollment
  (pending until a submitted code proves possession, AEAD-encrypted secret
  under MFA-specific key material — not the PHI keys), a Redis login
  challenge that withholds a session from an enforced account until that
  proof completes (with a per-account challenge epoch so a supervisor
  reset invalidates any challenge issued before it), ten salted-hash
  one-time backup codes, a supervisor-only reset that refuses
  self-approval, rate limiting, and audited enrollment/verify/reset events
  that never carry secret material. Rollout is config-driven
  (`off`/`prompt`/`enforce`, pilot scope, dated cutover, an emergency
  rollback override) and **ships `off`** — merging this does not enroll or
  enforce MFA for any account. Every existing/seeded account defaults to
  `mfa_shared_account = TRUE` and `mfa_pilot = FALSE`
  (migration 033) — deliberately fail-closed, since this repo has no
  staff-directory data to say which accounts are individually owned versus
  a shared login. Classifying the real roster, selecting pilot accounts,
  and setting rollout dates are deployment/client decisions this repo
  cannot make for them; see `docs/runbook.md` for the operator sequence.
- ~~Every account has a single flat role with no per-action authorization~~
  **Partially resolved.** Four real, enforced least-privilege roles now exist
  (`config/roles.yaml`: `front_desk`, `clinician`, `roi_clerk`, `scheduler`
  — see `services/gateway/roles_config.py`/`app.py`'s `require_permission`).
  The legacy `staff` role keeps its original full permission set, and every
  existing/seeded account is still on it; migrating real accounts to a
  specific role needs staff-directory/job-function data this repo doesn't
  have — an open question for the client, not guessed here.
- **Secrets are committed** (`.env` is tracked); CI has no secret/vuln scan.
  Both still open. `.env` stays committed per standing instruction and is
  flagged as a pre-go-live deployment decision, not a code change.

## 8. Local development

See `README.md` (quick start) and `docs/runbook.md` (operations + recovery).
