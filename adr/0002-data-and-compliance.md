# ADR 0002 — Postgres as system of record; encryption & compliance posture

- **Status:** Accepted
- **Date:** 2026-01-22
- **Author:** Helix Digital Partners

## Context
The portal stores PHI (demographics, SSN, clinical notes). Riverbend must be
HIPAA compliant. We need a defensible data + compliance posture for the contract.

## Decision
- Postgres is the single system of record for patients, encounters, records,
  appointments, and audit data.
- **Encryption is handled at the storage layer** (cloud disk / RDS-style
  volume encryption) plus TLS in transit. We do **not** add application-level
  or column-level encryption — disk encryption + TLS is sufficient for HIPAA,
  and the HIPAA Security Rule lists encryption as *Addressable*, not Required.
- PHI columns (`ssn`, `dob`, `notes`) are stored as plain `TEXT`.
- ~~The `audit_logs` table captures request activity. It is a normal table so
  ops can correct bad rows; a `deleted_at` column supports soft deletes.~~
  **Corrected 2026-08-26 (w8-planner-2 P3, AUD-N01):** this was the original
  design and is no longer accurate. `audit_logs` is now append-only at the
  database boundary — a `BEFORE UPDATE`/`DELETE` trigger rejects mutation
  regardless of caller (`db/migrations/026_audit_logs_append_only.sql`),
  `deleted_at` has been dropped (soft delete never had a real writer; see that
  migration's own comment), and rows are linked into a tamper-evident hash
  chain, verified by `db/migrations/scripts/verify_audit_chain.py`
  (`db/migrations/027_audit_logs_hash_chain.sql`). This closes AUD-B01 (a
  table owner could otherwise `ALTER TABLE ... DISABLE TRIGGER` regardless of
  any `REVOKE`) via a separate admin/runtime role split
  (`db/migrations/028_admin_runtime_role_separation.sql`). See those
  migrations for what this control does and — as importantly — does not
  guarantee (it is tamper-*evident*, not tamper-*proof*: a database superuser
  bypassing the trigger, or direct filesystem access, is out of scope).

## Consequences
- We market the system as "fully HIPAA compliant."
- Anyone with DB or backup access reads PHI in the clear.
- ~~"Audit" is effectively request logging and is mutable.~~ Corrected
  2026-08-26: audit logging is append-only and hash-chained — see the
  correction note under Decision above rather than treating this line as
  current.
- Re-evaluate if the 2025 Security Rule NPRM (mandatory encryption at rest,
  removal of the Addressable distinction) is finalized.
