#!/usr/bin/env python3
"""Verify audit_logs's tamper-evident hash chain (migration 027).

Walks every row in chain_position order (NOT id — see that migration for why
id is unsafe to link on: it is allocation order, not commit order) and
recomputes each one's expected chain_hash from its own metadata plus the
PRECEDING row's actual stored chain_hash — the exact formula
db/migrations/027_audit_logs_hash_chain.sql's trigger uses. Reports the first
break, if any: either a row whose chain_hash does not match what its own
content + the actual previous row's hash would produce, or a row whose
prev_chain_hash does not equal the actual previous row's chain_hash (a
spliced or reordered chain).

This proves detectability of content changes and internal gaps, not
prevention, and not detection of TAIL truncation — see that migration's own
comment for the full statement of what a tamper-evident (not tamper-proof)
chain does and does not guarantee. In particular: if the most recent N rows
were deleted and the chain simply stops there, the remaining chain still
verifies as intact, because there is nothing after the cut to reveal a
broken link. Detecting that requires an externally stored checkpoint this
script does not implement.

Never prints actor/message content, only chain positions and hash values —
those two fields are already confirmed metadata-only by every current
writer, but this script has no way to enforce that for a future one, so it
stays conservative regardless.
"""
import hashlib
import os
import sys

# The canonical timestamp string is produced entirely in SQL (see main()'s
# query) using the SAME to_char(... AT TIME ZONE 'UTC' ...) expression the
# trigger uses — this script never reformats a datetime itself, which is
# what makes the encoding "one unambiguous canonical encoding shared by
# PostgreSQL and Python": there is really only one implementation of the
# timestamp format, in SQL, used by both sides.


def encode_field(value):
    """Length-prefixed, NULL-explicit encoding — must match
    audit_logs_encode_field() in 027_audit_logs_hash_chain.sql exactly.
    NULL -> "N"; otherwise "<utf8 byte length>:<value>". Self-delimiting, so
    no field's own content (even one containing ":" or digits) can shift a
    later field's boundary."""
    if value is None:
        return "N"
    encoded = value.encode("utf-8")
    return f"{len(encoded)}:{value}"


def _row_hash(prev_hash, chain_position, actor, message, created_at_canonical):
    payload = (
        encode_field(prev_hash)
        + encode_field(str(chain_position))
        + encode_field(actor)
        + encode_field(message)
        + encode_field(created_at_canonical)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_chain(rows):
    """rows: iterable of (chain_position, actor, message, created_at_canonical,
    prev_chain_hash, chain_hash), already ordered by chain_position ascending.
    Returns (ok: bool, break_position: Optional[int], reason: Optional[str])."""
    actual_prev_hash = None
    for chain_position, actor, message, created_at_canonical, prev_chain_hash, chain_hash in rows:
        if prev_chain_hash != actual_prev_hash:
            return False, chain_position, "prev_chain_hash does not match the actual preceding row's chain_hash"
        expected = _row_hash(actual_prev_hash, chain_position, actor, message, created_at_canonical)
        if chain_hash != expected:
            return False, chain_position, "chain_hash does not match this row's own content"
        actual_prev_hash = chain_hash
    return True, None, None


def main(argv=None) -> int:
    try:
        import psycopg2
    except ImportError:
        print("psycopg2 is required.", file=sys.stderr)
        return 3

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL must be set.", file=sys.stderr)
        return 3

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:  # never print the exception: it can carry a DSN
        print(f"could not connect to the database ({type(exc).__name__}).", file=sys.stderr)
        return 3

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chain_position, actor, message, "
                "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), "
                "prev_chain_hash, chain_hash "
                "FROM audit_logs ORDER BY chain_position"
            )
            rows = cur.fetchall()
        conn.rollback()  # read-only; never hold a transaction open past the read
    finally:
        conn.close()

    if not rows:
        print("audit_logs is empty — nothing to verify.")
        return 0

    ok, break_position, reason = verify_chain(rows)
    if ok:
        print(f"OK — {len(rows)} row(s) verified, chain intact.")
        return 0

    print(f"CHAIN BROKEN at chain_position={break_position}: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
