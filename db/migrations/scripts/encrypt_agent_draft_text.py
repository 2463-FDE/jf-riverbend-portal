#!/usr/bin/env python3
"""One-time, operator-invoked backfill: encrypts every existing plaintext
agent_draft_provenance.generated_text row (adr/0012 follow-up, migration
032). Mirrors db/migrations/scripts/encrypt_existing_phi.py's pattern —
see that file's docstring for the general reasoning (why this is a
separate script from the SQL migration, why idempotent/resumable/batched
commits) — this one is scoped to a single column on a single table with a
different, stricter immutability trigger.

WHY THIS UPDATE IS ALLOWED AT ALL. agent_draft_provenance_guard_trigger
(migration 020, updated by 032) freezes generated_text on every UPDATE —
EXCEPT when generated_text_key_version changes in the same statement,
which this script always does (NULL -> the active version). That pairing
is exactly what the trigger treats as "a re-encryption, not a content
edit" — see migration 032's own comment for the invariant. A statement
here that tried to change generated_text WITHOUT also setting
generated_text_key_version would be rejected by the trigger, by design.

Also usable, unmodified, for a FUTURE key rotation: point it at rows with
generated_text_key_version = '<old version>' instead of IS NULL (see
--key-version) and it re-encrypts them under the current active version
the same way. Verifying the recovered plaintext is unchanged across a
rotation is THIS script's job (it decrypts under the OLD key and
re-encrypts under the NEW one — the plaintext only ever exists in this
process's memory, never compared against anything written elsewhere) —
the trigger has no key and cannot check that itself.

Deploy order (see adr/0012 and migration 032's own header): apply
migration 032, run this script ONCE (verify it reports zero remaining
plaintext rows), then deploy the new records-service code. Deploying the
new code before this script runs is actually SAFE here, unlike the
patients.ssn/dob/notes backfill — _draft_out already treats a NULL
generated_text_key_version as "this row is still plaintext, return
as-is" (services/records-service/phi.py::decrypt_draft_text) — but new
DRAFTS are always encrypted from creation regardless of backfill status,
so running this script promptly is still what actually removes plaintext
storage, not merely a formality.

Never prints a plaintext draft, a ciphertext envelope, or key material —
only draft ids, patient ids, versions, and counts.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from libs.phi_crypto import EnvKeyProvider, KeyConfigurationError, decrypt, encrypt  # noqa: E402

_BATCH_SIZE = 500


def _dsn_from_split_vars():
    """Same DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD fallback as
    encrypt_existing_phi.py — see that function's docstring. This script
    runs inside records-service's own container (it owns
    generated_text/generated_text_key_version end to end), so these are
    the vars records-service's own config.py already reads."""
    user = os.getenv("DB_USER")
    if not user:
        return None
    host = os.getenv("DB_HOST", "postgres")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "riverbend")
    password = os.getenv("DB_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def _aad(patient_id: int, version: int) -> bytes:
    # Must match services/records-service/phi.py::_draft_aad exactly — a
    # mismatch here would make every row this script writes fail to
    # decrypt later.
    return f"agent_draft_provenance.generated_text.{patient_id}.{version}".encode("utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-version",
        default=None,
        help=(
            "Re-encrypt rows currently under this key version instead of the "
            "default (rows with a NULL generated_text_key_version, i.e. the "
            "initial plaintext backfill). Use for a future key rotation."
        ),
    )
    args = parser.parse_args(argv[1:] if argv else None)

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
        print(f"PHI key configuration is invalid: {exc}", file=sys.stderr)
        return 3

    active_version = key_provider.active_key_version()
    source_version = args.key_version

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:  # never print the exception: it can carry a DSN
        print(f"could not connect to the database ({type(exc).__name__}).", file=sys.stderr)
        return 3

    total_updated = 0
    try:
        with conn.cursor() as select_cur:
            if source_version is None:
                select_cur.execute(
                    "SELECT id, patient_id, version, generated_text FROM agent_draft_provenance "
                    "WHERE generated_text_key_version IS NULL ORDER BY id"
                )
            else:
                select_cur.execute(
                    "SELECT id, patient_id, version, generated_text FROM agent_draft_provenance "
                    "WHERE generated_text_key_version = %s ORDER BY id",
                    (source_version,),
                )
            rows = select_cur.fetchall()

        if not rows:
            print("no plaintext agent_draft_provenance rows found — nothing to backfill.")
            return 0

        print(f"{len(rows)} draft row(s) need re-encryption to key version {active_version!r}.")

        with conn.cursor() as update_cur:
            for i, (row_id, patient_id, version, stored_text) in enumerate(rows, start=1):
                if source_version is None:
                    plaintext = stored_text
                else:
                    plaintext = decrypt(
                        stored_text, key_provider.encryption_key(source_version), _aad(patient_id, version)
                    )
                new_envelope = encrypt(plaintext, key_provider.encryption_key(active_version), _aad(patient_id, version))
                update_cur.execute(
                    "UPDATE agent_draft_provenance SET generated_text = %s, generated_text_key_version = %s "
                    "WHERE id = %s",
                    (new_envelope, active_version, row_id),
                )
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

    print(f"OK — re-encrypted {total_updated} draft row(s) to key version {active_version!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
