-- 027_audit_logs_hash_chain.sql — P3 audit integrity, tamper-evident chain
-- (w8-planner-2). Stacked on 026: the append-only trigger there is what
-- makes this chain meaningful — a hash chain over a table that could still
-- be freely UPDATEd/DELETEd would prove nothing, since an attacker could
-- simply recompute the whole chain after tampering.
--
-- WHAT THIS DETECTS. A row whose own content changed after insertion (its
-- stored chain_hash no longer matches a recompute over its own metadata),
-- or a row removed/spliced from the MIDDLE or START of the chain (the next
-- surviving row's prev_chain_hash no longer matches any real row's actual
-- chain_hash). Both are proven by db/migrations/scripts/verify_audit_chain.py
-- walking the chain in chain_position order.
--
-- WHAT THIS DOES NOT DETECT. Truncation at the TAIL — deleting the most
-- recent N rows and stopping there — leaves the remaining chain internally
-- consistent end-to-end; there is nothing after the cut to reveal a broken
-- link, and this table has no way to know how many rows SHOULD exist.
-- Detecting that requires an externally stored checkpoint (a chain_hash +
-- chain_position recorded somewhere OUTSIDE this database, on a schedule)
-- to compare "does the chain still reach at least that point" — this
-- migration does not add one. Nor is this tamper-PROOF: a database
-- superuser bypassing 026's trigger entirely, or direct filesystem access
-- to the Postgres data files, could still corrupt the chain. This
-- control's job is to make ordinary in-database tampering provable when
-- someone runs the verifier, not to prevent every possible attacker.
--
-- CHAIN_POSITION, NOT id. `id` is a bare SERIAL: nextval() can be consumed
-- by a transaction that later rolls back (a gap that was never a committed
-- row), and two concurrently inserting transactions can be assigned ids in
-- one order but COMMIT in the other — id order is allocation order, not
-- commit order. chain_position is a separate, dense, gap-free sequence
-- assigned only once a transaction has proven — via the advisory lock
-- below — that it is genuinely next in commit order. Linking and
-- verification both use chain_position, never id.
--
-- SERIALIZING CONCURRENT INSERTS. The BEFORE INSERT trigger takes
-- pg_advisory_xact_lock(hashtext('audit_logs_chain_lock')::bigint) before
-- reading the current chain tail — the same hashtext-keyed advisory-lock
-- pattern services/intake-service/app.py already uses for duplicate-patient
-- serialization, here with one fixed key shared by every insert into this
-- table rather than a per-value key. A second concurrent INSERT blocks
-- until the first transaction commits or rolls back and its (transaction-
-- scoped, auto-released) lock is freed — at which point the second
-- transaction's own SELECT sees the first one's committed row, or, if it
-- rolled back, no row and no consumed chain_position at all. A plain
-- `SELECT max(chain_position)` with no lock would let two concurrent
-- transactions both read the same "current tail" and compute two
-- conflicting rows claiming the same next position.
--
-- CANONICAL ENCODING. Every hashed field goes through
-- audit_logs_encode_field(), implemented identically here and in
-- verify_audit_chain.py's encode_field():
--   NULL      -> the single byte "N"
--   non-NULL  -> <utf8 byte length>":"<value>
-- Fields are concatenated in a fixed order with no separator between them —
-- none is needed, since each field's own length prefix is self-delimiting
-- regardless of what characters the field's value contains (a message
-- containing "5:hi|3:bye" cannot shift a field boundary, because the outer
-- length prefix says exactly how many bytes belong to THIS field). This
-- replaces an earlier version of this migration that joined fields with a
-- bare "|" — collision-prone if any field's own content could ever contain
-- "|", and conflated NULL with empty string.
--
-- created_at is hashed as the UTC-normalised, fixed-precision string
-- to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
-- produces — the SAME expression the verifier's own SELECT uses, so the
-- Python side never reformats a datetime itself; it only ever treats this
-- as an opaque, already-canonical string field. TIMESTAMPTZ stores an
-- absolute instant regardless of session timezone, but a bare ::text cast
-- renders it in whatever timezone the CONNECTION happens to be in —
-- forcing UTC here means the hash does not depend on who is asking.
--
-- HASH INPUT IS METADATA ONLY, FOR CURRENT WRITERS — NOT A DB-ENFORCED
-- GUARANTEE. prev_chain_hash, chain_position, actor, message, created_at —
-- the exact same fields every current writer already logs (confirmed
-- metadata-only in 026's own commit, by reading every call site). Hashing
-- raw PHI would itself be the leak this repo's PHI-safe-logging policy
-- exists to prevent; a chain hash over metadata proves the metadata wasn't
-- altered without ever needing PHI content to do it. This is a property of
-- current CODE, not a constraint this table enforces — `message` is a plain
-- TEXT column, so nothing stops a future writer from putting PHI in it;
-- keep it metadata-only when adding one. A database that predates this
-- fix could have carried a raw-PHI historical row (db/seed/generate_seed.py
-- once did, on purpose, as a pre-DEBT-D1 teaching fixture) — 026 now
-- performs a one-time, targeted scrub of that exact known row BEFORE this
-- migration ever runs, so no chain computed by this trigger ever hashes it
-- (code review AUD-M01). That scrub is pre-chain data hygiene, not editing
-- an already-chained row after the fact — the tampering concern below is
-- about mutating a row THIS chain already covers, which 026's ordering
-- avoids entirely for that row. `ssn`/`dob`/notes remain plaintext in their
-- own columns regardless (adr/0002); this does not change that exposure,
-- only whether the metadata log is provably unaltered going forward.
--
-- MIGRATING EXISTING ROWS. A table that already had rows before this
-- migration first ran has no chain_position/chain_hash yet. This file
-- backfills them under one ACCESS EXCLUSIVE table lock, deterministically
-- in id order (the only ordering pre-existing rows actually have), with
-- 026's UPDATE-rejection trigger temporarily disabled for that backfill
-- (DELETE stays rejected throughout — only the mutation trigger is
-- touched, and only for the duration of this migration's own transaction).
-- NOT NULL and a UNIQUE constraint on chain_position are added only after
-- every row has one. Re-running this migration is a no-op: the backfill
-- only ever targets rows where chain_position IS NULL, so an
-- already-migrated table has nothing left to backfill, and every DDL
-- statement below is itself guarded to be idempotent (see apply.sh).

BEGIN;

-- Pinned to public explicitly: CREATE EXTENSION with no SCHEMA clause
-- installs into whatever schema is first in the CURRENT search_path, which
-- is not always public (e.g. under an isolated test schema). Since
-- IF NOT EXISTS treats "pgcrypto exists anywhere in this database" as
-- already satisfied, an accidental non-public install would silently make
-- digest() invisible to any session using the ordinary default search_path
-- from then on. Pinning avoids that drift regardless of where this
-- migration happens to run from.
CREATE EXTENSION IF NOT EXISTS pgcrypto SCHEMA public;

ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS chain_position INTEGER;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS prev_chain_hash TEXT;
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS chain_hash TEXT;

CREATE OR REPLACE FUNCTION audit_logs_encode_field(value TEXT) RETURNS TEXT AS $$
BEGIN
    IF value IS NULL THEN
        RETURN 'N';
    END IF;
    RETURN octet_length(value)::text || ':' || value;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Explicit, even though ADD COLUMN above already implied it for the rest of
-- this transaction — documents the intent this migration must hold for the
-- whole backfill+constraints sequence, and defends against a future reorder.
LOCK TABLE audit_logs IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'audit_logs_no_update' AND tgrelid = 'audit_logs'::regclass
    ) THEN
        ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_update;
    END IF;
END $$;

-- Backfill every row that doesn't have a chain_position yet, deterministically
-- in id order, resuming from wherever the chain currently ends (NULL/0 for a
-- table with no chained rows at all — including a fresh, empty table, where
-- this loop simply does nothing).
DO $$
DECLARE
    r RECORD;
    running_prev_hash TEXT;
    running_position INTEGER;
    canonical_created_at TEXT;
    payload TEXT;
    new_hash TEXT;
BEGIN
    SELECT chain_position, chain_hash INTO running_position, running_prev_hash
        FROM audit_logs
        WHERE chain_position IS NOT NULL
        ORDER BY chain_position DESC
        LIMIT 1;

    running_position := coalesce(running_position, 0);

    FOR r IN
        SELECT id, actor, message, created_at FROM audit_logs
        WHERE chain_position IS NULL
        ORDER BY id ASC
    LOOP
        running_position := running_position + 1;
        canonical_created_at := to_char(r.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
        payload :=
            audit_logs_encode_field(running_prev_hash) ||
            audit_logs_encode_field(running_position::text) ||
            audit_logs_encode_field(r.actor) ||
            audit_logs_encode_field(r.message) ||
            audit_logs_encode_field(canonical_created_at);
        new_hash := encode(digest(payload, 'sha256'), 'hex');

        UPDATE audit_logs
        SET chain_position = running_position,
            prev_chain_hash = running_prev_hash,
            chain_hash = new_hash
        WHERE id = r.id;

        running_prev_hash := new_hash;
    END LOOP;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'audit_logs_no_update' AND tgrelid = 'audit_logs'::regclass
    ) THEN
        ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_no_update;
    END IF;
END $$;

ALTER TABLE audit_logs ALTER COLUMN chain_position SET NOT NULL;
ALTER TABLE audit_logs ALTER COLUMN chain_hash SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'audit_logs_chain_position_unique' AND conrelid = 'audit_logs'::regclass
    ) THEN
        ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_chain_position_unique UNIQUE (chain_position);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION audit_logs_compute_chain_hash() RETURNS TRIGGER AS $$
DECLARE
    prev_position INTEGER;
    prev_hash TEXT;
    next_position INTEGER;
    canonical_created_at TEXT;
    payload TEXT;
BEGIN
    -- See "SERIALIZING CONCURRENT INSERTS" above. Transaction-scoped: this
    -- lock releases automatically at COMMIT/ROLLBACK, no explicit unlock.
    PERFORM pg_advisory_xact_lock(hashtext('audit_logs_chain_lock')::bigint);

    SELECT chain_position, chain_hash INTO prev_position, prev_hash
        FROM audit_logs ORDER BY chain_position DESC LIMIT 1;

    next_position := coalesce(prev_position, 0) + 1;
    NEW.chain_position := next_position;
    NEW.prev_chain_hash := prev_hash;  -- NULL only for the genesis row

    canonical_created_at := to_char(NEW.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');

    payload :=
        audit_logs_encode_field(prev_hash) ||
        audit_logs_encode_field(next_position::text) ||
        audit_logs_encode_field(NEW.actor) ||
        audit_logs_encode_field(NEW.message) ||
        audit_logs_encode_field(canonical_created_at);

    NEW.chain_hash := encode(digest(payload, 'sha256'), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_logs_chain_before_insert ON audit_logs;
CREATE TRIGGER audit_logs_chain_before_insert
    BEFORE INSERT ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_compute_chain_hash();

COMMIT;
