#!/usr/bin/env python3
"""One-time, operator-invoked backfill: encrypts every existing plaintext
patients.ssn/dob/notes row and repopulates patients.ssn_digits as an
HMAC-SHA256 blind index (w8-planner-2 P2, adr/0012).

Why this is a separate script, not part of db/migrations/031's SQL: a
SQL-only migration has no way to reach PHI_ENCRYPTION_KEY_V1/
PHI_BLIND_INDEX_KEY_V1 or run AEAD encryption — that only exists in
libs/phi_crypto, a Python package. Migration 031 only does the DDL (column
additions, dropping ssn_digits' GENERATED expression); this script does the
actual data migration on top of it.

Deploy order (see adr/0012): apply migration 031, run this script ONCE,
THEN deploy the new intake-service/records-service code. Deploying the new
code before this script runs makes records-service try to AEAD-decrypt
still-plaintext ssn/dob/notes and fail closed on every row — the new code
has no plaintext fallback by design (see phi.py's docstring in each
service: a NULL key_version alongside a non-NULL value is treated as "this
row IS still plaintext," which is exactly the pre-backfill state, but the
new code stops writing that state going forward — it exists only for rows
this script has not reached yet, mid-run).

Idempotent and resumable: only touches a row's ssn/dob/notes when that
field's own <field>_key_version is still NULL, so re-running after a
partial failure (or to pick up rows created between migration 031 and this
script's first run) only processes what is still plaintext. Commits in
batches, not one giant transaction, so a mid-run failure keeps whatever
was already committed rather than losing all progress.

Also the fix for a review finding on the PR that introduced this file
(w8-planner-2 P2 round 1, B1): `db/seed/seed.sql` is loaded verbatim by
Postgres's own docker-entrypoint-initdb.d on a fresh volume — plain SQL,
no way to reach libs/phi_crypto from inside that init sequence — so a
fresh `make up`/`make seed` leaves the seeded demo patients (including
the canonical Maria Gonzalez duplicate cluster, adr/0004) plaintext with
NULL key_versions and NULL ssn_digits, which breaks blind-index-based
duplicate detection for every seeded row. `make up` and `make seed`
(Makefile) now run this script — via `docker compose exec intake-service
python3 db/migrations/scripts/encrypt_existing_phi.py`, since that
container already has libs/phi_crypto, cryptography, DB connectivity,
and (once configured) the PHI_* keys — right after the schema/seed load,
using DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD (see
_dsn_from_split_vars) rather than requiring a separate DATABASE_URL wired
in just for this script. Idempotent, so running it on every `make up`
(not just the first, fresh-volume one) is harmless — see below.

Never prints a plaintext PHI value, a ciphertext envelope, a blind index,
or key material — only patient ids and counts.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from libs.phi_crypto import EnvKeyProvider, KeyConfigurationError, compute_blind_index, encrypt, normalize_ssn  # noqa: E402

_BATCH_SIZE = 500


def _aad(column: str, patient_id: int) -> bytes:
    # Must match services/intake-service/phi.py and
    # services/records-service/phi.py's _aad exactly — a mismatch here
    # would make every row this script writes fail to decrypt later.
    return f"patients.{column}.{patient_id}".encode("utf-8")


def _encrypt_row(key_provider, row_id, ssn, dob, notes):
    """Returns a dict of only the columns that need updating for this row
    (fields already migrated — key_version already set — are omitted, not
    re-encrypted; re-encrypting an already-migrated field would rotate its
    nonce for no reason and is simply unnecessary work, not unsafe, but
    skipping it keeps a partial re-run cheap)."""
    version = key_provider.active_key_version()
    updates = {}

    if ssn is not None:
        updates["ssn"] = encrypt(ssn, key_provider.encryption_key(version), _aad("ssn", row_id))
        updates["ssn_key_version"] = version
        normalized = normalize_ssn(ssn)
        updates["ssn_digits"] = (
            compute_blind_index(normalized, key_provider.blind_index_key(version)) if normalized else None
        )

    if dob is not None:
        updates["dob"] = encrypt(dob, key_provider.encryption_key(version), _aad("dob", row_id))
        updates["dob_key_version"] = version

    if notes is not None:
        updates["notes"] = encrypt(notes, key_provider.encryption_key(version), _aad("notes", row_id))
        updates["notes_key_version"] = version

    return updates


def _dsn_from_split_vars():
    """Falls back to DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD — the same
    split env vars every service's own config.py already reads (see e.g.
    services/intake-service/config.py) — so this script runs unmodified
    inside any of those containers (`docker compose exec intake-service
    python3 db/migrations/scripts/encrypt_existing_phi.py`) without needing
    a separate DATABASE_URL wired in just for it. Returns None (not a
    DSN with an empty password) if DB_USER isn't set at all, so the
    DATABASE_URL-required error above still fires with a clear message
    instead of psycopg2 failing on a nonsense connection string."""
    user = os.getenv("DB_USER")
    if not user:
        return None
    host = os.getenv("DB_HOST", "postgres")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "riverbend")
    password = os.getenv("DB_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def _update_row(cur, row_id, updates):
    if not updates:
        return
    set_clause = ", ".join(f"{col} = %s" for col in updates)
    cur.execute(f"UPDATE patients SET {set_clause} WHERE id = %s", [*updates.values(), row_id])


def main(argv=None) -> int:
    try:
        import psycopg2
    except ImportError:
        print("psycopg2 is required.", file=sys.stderr)
        return 3

    dsn = os.getenv("DATABASE_URL") or _dsn_from_split_vars()
    if not dsn:
        print("DATABASE_URL (or DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD) must be set.", file=sys.stderr)
        return 3

    try:
        key_provider = EnvKeyProvider()
    except KeyConfigurationError as exc:
        # KeyConfigurationError messages never carry a value, per
        # libs/phi_crypto/errors.py's contract — safe to print directly.
        print(f"PHI key configuration is invalid: {exc}", file=sys.stderr)
        return 3

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:  # never print the exception: it can carry a DSN
        print(f"could not connect to the database ({type(exc).__name__}).", file=sys.stderr)
        return 3

    total_updated = 0
    try:
        with conn.cursor() as select_cur:
            select_cur.execute(
                "SELECT id, ssn, dob, notes FROM patients "
                "WHERE (ssn IS NOT NULL AND ssn_key_version IS NULL) "
                "   OR (dob IS NOT NULL AND dob_key_version IS NULL) "
                "   OR (notes IS NOT NULL AND notes_key_version IS NULL) "
                "ORDER BY id"
            )
            rows = select_cur.fetchall()

        if not rows:
            print("no plaintext patients rows found — nothing to backfill.")
            return 0

        print(f"{len(rows)} patient row(s) need backfilling.")

        with conn.cursor() as update_cur:
            for i, (row_id, ssn, dob, notes) in enumerate(rows, start=1):
                updates = _encrypt_row(key_provider, row_id, ssn, dob, notes)
                _update_row(update_cur, row_id, updates)
                total_updated += 1
                if i % _BATCH_SIZE == 0:
                    conn.commit()
                    print(f"  committed {i}/{len(rows)}")
            conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"backfill failed after {total_updated} row(s) ({type(exc).__name__}) — safe to re-run.", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(f"OK — backfilled {total_updated} patient row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
