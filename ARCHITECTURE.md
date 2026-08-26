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
  (no TTL on the Redis key; `auth.yaml SESSION_TIMEOUT: never`). MFA is off.

See `adr/0003-authentication-and-sessions.md`.

## 5. Data model

Postgres 15 is the single system of record. Flattened schema:
`db/schema.sql`. Ordered forward migrations: `db/migrations/00N_*.sql`
(hand-rolled; kept in sync with `schema.sql` by hand). Demo data is generated
deterministically by `db/seed/generate_seed.py` → `db/seed/seed.sql`
(~250 patients, ~475 encounters, ~690 records, plus appointments, slots,
insurance, ROI requests, and audit rows).

**There is no encryption anywhere in this system.** PHI columns (`ssn`,
`notes`, `dob`) are plain `TEXT`, and `ssn_digits` is a generated, indexed copy
of the SSN. Nothing is encrypted at the storage layer — the deployment is
docker compose with a local `pgdata` volume — and no hop uses TLS, including
`/login`. This paragraph previously claimed storage-layer encryption and TLS in
transit; both were false. See `adr/0008` for the recorded risk decision and
`adr/0002` for the original data-and-compliance discussion.

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

- **Compliance posture is self-asserted.** PHI columns are plaintext (`adr/0002`,
  unchanged). ~~"audit" is still mutable request logging, not a tamper-evident
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
- **N+1 + full-table scans** in the records read/search paths. Still open,
  deliberately deferred (`docs/analysis/W4-records-N-plus-one.md`, DEBT D8).
  Needs fixing alongside the missing patient-scoped indexes, not before them.
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
- **No MFA — still open, deferred to next cycle by client direction
  (2026-08-12).** A TOTP second factor was built and tested, then parked to
  be delivered as one complete rollout rather than a bare mechanism. The
  prototype is on `feat/mfa-totp-parked`, unmerged and incomplete against
  the agreed scope. `/login` in the merged tree is password-only.
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
