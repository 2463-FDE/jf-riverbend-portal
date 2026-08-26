-- 026_audit_logs_append_only.sql — P3 audit integrity, database-boundary
-- control (w8-planner-2).
--
-- audit_logs has been UPDATE/DELETE-able since 001_init.sql, and
-- services/records-service/models.py's AuditLog docstring is explicit that
-- this is "NOT a tamper-evident access trail." This closes the mutability
-- half of that gap at the database boundary. A tamper-evident hash chain
-- and its verifier are separate, later work — this migration does not add
-- one, and this table must not be described as tamper-evident until it does.
--
-- WHY A TRIGGER, NOT A REVOKE. All services share one Postgres credential
-- (riverbend_app, adr/0001) with no per-service least privilege — a REVOKE
-- UPDATE/DELETE FROM riverbend_app would have NO EFFECT here, because
-- riverbend_app owns audit_logs (it ran the CREATE TABLE), and Postgres
-- table owners bypass GRANT/REVOKE privilege checks on their own objects
-- entirely. A BEFORE UPDATE/DELETE trigger has no such escape hatch against
-- an ordinary GRANT/REVOKE-based caller — it fires regardless of which
-- role is connected. Corrected (code review AUD-B01): the table's OWNER
-- can still bypass it by issuing `ALTER TABLE ... DISABLE TRIGGER` first,
-- since ALTER TABLE checks ownership, not grantable privileges — and
-- riverbend_app owned this table until
-- db/migrations/028_admin_runtime_role_separation.sql (stacked prerequisite,
-- PR #85) made it a non-owner. This trigger and that ownership split are
-- both required; neither alone closes the gap.
--
-- deleted_at is DROPPED, not merely stopped-being-set. Nothing in this
-- codebase ever set or filtered on it (grepped the whole tree — only its
-- own declaration in schema.sql/001_init.sql and its ORM mapping
-- referenced it at all), and once UPDATE is rejected outright, no future
-- code path could set it either — keeping a column that can never again be
-- written implies a soft-delete capability that no longer exists.
--
-- INSERT is untouched. New audit rows must keep working; only mutating or
-- removing an existing one is rejected. Guarded/idempotent throughout, so
-- this is safe to re-apply against a database at any prior migration point
-- (see apply.sh).
--
-- ONE-TIME DATA REMEDIATION (code review AUD-M01, 2026-08-26). An earlier
-- revision of db/seed/generate_seed.py preserved a raw-PHI audit_logs row
-- (a patient's name/DOB/SSN inside a fake request-body message) as a
-- deliberate demonstration of the pre-DEBT-D1 logging bug. Once 027 starts
-- hashing audit_logs content into a permanent tamper-evident chain, leaving
-- that row in place would bake PHI into a hash forever — worse than the
-- plaintext PHI columns this schema already has, not equivalent debt, and
-- not something to carry forward. This UPDATE runs BEFORE the append-only
-- triggers below take effect (it targets the exact known legacy row by its
-- distinguishing content, so it is a no-op — 0 rows — everywhere else,
-- including a database that already has the scrubbed version from a
-- regenerated seed.sql). The replacement text matches the metadata-only
-- shape services/intake-service/app.py's real _INTAKE_LOG_SUMMARY_KEYS
-- actually logs (correlation_id, created_via) — see generate_seed.py for
-- the same text used on a fresh seed.
UPDATE audit_logs
SET message = 'POST /intake correlation_id=seed-demo-0001 created_via=self_service'
WHERE actor = 'intake-service'
  AND message LIKE 'POST /intake body=%Maria Gonzalez%';

ALTER TABLE audit_logs DROP COLUMN IF EXISTS deleted_at;

CREATE OR REPLACE FUNCTION audit_logs_reject_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs;
CREATE TRIGGER audit_logs_no_update
    BEFORE UPDATE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation();

DROP TRIGGER IF EXISTS audit_logs_no_delete ON audit_logs;
CREATE TRIGGER audit_logs_no_delete
    BEFORE DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation();
