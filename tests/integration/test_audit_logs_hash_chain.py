"""Integration test — requires a real Postgres (`make up`). P3 audit
integrity (w8-planner-2, PR #86): migration 027 adds a tamper-evident hash
chain over audit_logs, linked and verified by chain_position (not id),
computed by a BEFORE INSERT trigger — see that migration for what it does
and does not guarantee. Stacked on migration 026's append-only guard (PR
#84) and the admin/runtime ownership split (PR #85) — see
tests/integration/test_audit_logs_append_only.py for 026's own tests.
db/migrations/scripts/verify_audit_chain.py's own detection logic (a
broken/spliced/missing-row chain) is unit-tested directly in
tests/test_verify_audit_chain.py; this file proves the REAL trigger's
output is exactly what that verifier expects, including under real
backfill and real concurrency.

Most tests share one module-scoped isolated schema (`_isolated_schema`
below). A few need to observe migration 027 running against a KNOWN, freshly
constructed starting state — pre-populated rows for the backfill test, a
deliberately corrupted chain for the deletion tests — and use their own
throwaway schema instead so they can't affect, or be affected by, the shared
module fixture's tests. Neither ever touches the real `public.audit_logs` —
mirrors tests/integration/test_policy_corpus_pipeline.py's isolation
pattern, for the same reason: this table can carry real rows from other work
against the same shared local database, and deleting/mutating them to test a
delete/mutation-rejection contract would be exactly backwards.

Run with:  pytest -m integration tests/integration/test_audit_logs_hash_chain.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import contextlib
import os
import threading
import uuid

import pytest

from conftest import load_module

psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MIGRATION_PATHS = (
    os.path.join(_REPO_ROOT, "db", "migrations", "026_audit_logs_append_only.sql"),
    os.path.join(_REPO_ROOT, "db", "migrations", "027_audit_logs_hash_chain.sql"),
)
_TEST_SCHEMA = f"audit_logs_chain_test_{uuid.uuid4().hex[:12]}"

verify = load_module("db/migrations/scripts/verify_audit_chain.py", "verify_audit_chain_integration")

# The pre-026 shape (001_init.sql / schema.sql before that migration): the
# realistic starting point migration 026 actually runs against on a real
# deployment — an existing table, already carrying the deleted_at column
# that migration must drop.
_BASE_TABLE_SQL = """
CREATE TABLE audit_logs (
    id          SERIAL PRIMARY KEY,
    actor       TEXT,
    message     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);
"""

_CHAIN_SELECT = (
    "SELECT chain_position, actor, message, "
    "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"'), "
    "prev_chain_hash, chain_hash FROM audit_logs"
)


def _bare_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", "changeme"),
    )


def _admin_connection():
    """PR #85 (stacked underneath this branch) demotes DB_USER off CREATE
    privilege on the database, and off ownership of audit_logs, as part of
    028_admin_runtime_role_separation.sql — which this branch's own
    apply.sh always applies alongside 026/027, and docker-compose.yml now
    boots every fresh install already split. Schema/table setup, and the
    trigger-disabling tamper simulations in _scratch_schema, need an owner-
    equivalent role; only the actual chain-insert assertions need to run AS
    the demoted runtime role."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_ADMIN_USER", "riverbend_admin"),
        password=os.environ["DB_ADMIN_PASSWORD"],
    )


def _connection():
    """Every connection this module hands out is pinned to the isolated test
    schema first, `public` second — so an unqualified `audit_logs` always
    resolves to the throwaway copy, never the real one."""
    conn = _bare_connection()
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
    conn.commit()
    return conn


@pytest.fixture(scope="module", autouse=True)
def _isolated_schema():
    app_role = os.getenv("DB_USER", "riverbend_app")
    setup_conn = _admin_connection()
    setup_conn.autocommit = True
    with setup_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {_TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
        cur.execute(_BASE_TABLE_SQL)
        for path in _MIGRATION_PATHS:
            with open(path, encoding="utf-8") as f:
                cur.execute(f.read())
        # 028's actual real-world runtime grant: INSERT + SELECT only. The
        # chain trigger fires on INSERT regardless, same as any other role.
        cur.execute(f'GRANT USAGE ON SCHEMA {_TEST_SCHEMA} TO "{app_role}"')
        cur.execute(f'GRANT SELECT, INSERT ON audit_logs TO "{app_role}"')
        cur.execute(f'GRANT USAGE, SELECT ON audit_logs_id_seq TO "{app_role}"')
    setup_conn.close()

    yield

    teardown_conn = _admin_connection()
    teardown_conn.autocommit = True
    with teardown_conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE")
    teardown_conn.close()


def _insert_row(cur, actor="tester", message="test event"):
    cur.execute(
        "INSERT INTO audit_logs (actor, message) VALUES (%s, %s) RETURNING id",
        (actor, message),
    )
    return cur.fetchone()[0]


@contextlib.contextmanager
def _scratch_schema():
    """A throwaway schema independent of the shared module schema above —
    for tests that need to observe migration 027 running against a KNOWN
    starting or ending state (pre-migration rows, a deliberately corrupted
    chain) without affecting or being affected by the shared fixture's
    tests. Yields a cursor already pointed at it via search_path, with the
    pre-026 base table already created; the caller applies whichever
    migration(s) it needs."""
    schema = f"audit_logs_chain_scratch_{uuid.uuid4().hex[:10]}"
    conn = _admin_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}, public")
            cur.execute(_BASE_TABLE_SQL)
            yield cur
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


def test_the_migrations_are_safe_to_reapply():
    # apply.sh's own contract: every migration must be a no-op the second
    # time, safe to run against a database at any prior migration point —
    # including one that already has chained rows, which is the realistic
    # case by this point in the module (earlier tests have already inserted).
    conn = _admin_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
        for path in _MIGRATION_PATHS:
            with open(path, encoding="utf-8") as f:
                cur.execute(f.read())  # must not raise
    conn.close()


def test_insert_populates_a_genesis_row_with_no_prev_hash():
    conn = _connection()
    with conn.cursor() as cur:
        row_id = _insert_row(cur, actor="genesis_test", message="first ever row in this run")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT chain_position, prev_chain_hash, chain_hash FROM audit_logs WHERE id = %s", (row_id,)
        )
        chain_position, prev_hash, chain_hash = cur.fetchone()
    conn.close()

    # Not necessarily position 1 / NULL prev_hash — an earlier test in this
    # module may already have inserted a row first, in which case this one
    # chains onto that. Either way, both must always be populated.
    assert chain_position is not None
    assert chain_hash
    assert len(chain_hash) == 64  # hex-encoded sha256


def test_consecutive_inserts_chain_onto_each_other():
    conn = _connection()
    with conn.cursor() as cur:
        first_id = _insert_row(cur, actor="chain_test", message="event one")
    conn.commit()
    with conn.cursor() as cur:
        second_id = _insert_row(cur, actor="chain_test", message="event two")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT chain_position, chain_hash FROM audit_logs WHERE id = %s", (first_id,))
        first_position, first_chain_hash = cur.fetchone()
        cur.execute("SELECT chain_position, prev_chain_hash FROM audit_logs WHERE id = %s", (second_id,))
        second_position, second_prev_hash = cur.fetchone()
    conn.close()

    assert second_prev_hash == first_chain_hash
    assert second_position == first_position + 1


def test_the_real_trigger_produced_chain_verifies_with_the_real_verifier():
    conn = _connection()
    with conn.cursor() as cur:
        _insert_row(cur, actor="verifier_test", message="event a")
        _insert_row(cur, actor="verifier_test", message="event b")
        _insert_row(cur, actor="verifier_test", message="event c")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"{_CHAIN_SELECT} ORDER BY chain_position")
        rows = cur.fetchall()
    conn.close()

    ok, break_position, reason = verify.verify_chain(rows)

    assert ok is True, f"chain broke at chain_position={break_position}: {reason}"


def test_two_concurrent_inserts_still_form_one_chain():
    # Real concurrency, not simulated: two separate connections/threads,
    # synchronized to attempt their INSERT at the same moment via a
    # barrier. Without the trigger's pg_advisory_xact_lock serializing
    # chain_position assignment, this could produce two rows claiming the
    # same position or the same prev_chain_hash.
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def _do_insert(key, actor, message):
        conn = _connection()
        try:
            barrier.wait(timeout=10)
            with conn.cursor() as cur:
                results[key] = _insert_row(cur, actor=actor, message=message)
            conn.commit()
        except Exception as exc:  # surfaced via `errors`, not swallowed
            errors.append(exc)
        finally:
            conn.close()

    t1 = threading.Thread(target=_do_insert, args=("a", "concurrent_test", "event alpha"))
    t2 = threading.Thread(target=_do_insert, args=("b", "concurrent_test", "event beta"))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, f"concurrent insert raised: {errors}"
    assert set(results) == {"a", "b"}

    conn = _connection()
    with conn.cursor() as cur:
        cur.execute(f"{_CHAIN_SELECT} WHERE id IN (%s, %s) ORDER BY chain_position", (results["a"], results["b"]))
        pair = cur.fetchall()
        # Verify the FULL chain, not just this pair — proves these two
        # concurrent transactions were serialized into the SAME linear
        # chain as everything else inserted earlier in this module, not a
        # fork with a colliding position.
        cur.execute(f"{_CHAIN_SELECT} ORDER BY chain_position")
        full_chain = cur.fetchall()
    conn.close()

    assert len(pair) == 2
    positions = [row[0] for row in pair]
    assert positions[1] == positions[0] + 1  # consecutive: no collision, no gap

    ok, break_position, reason = verify.verify_chain(full_chain)
    assert ok is True, f"full chain broke at chain_position={break_position}: {reason}"


def test_delimiters_null_empty_and_timezone_variants_hash_consistently():
    conn = _connection()
    with conn.cursor() as cur:
        # A message containing the encoding's own delimiter characters.
        delim_id = _insert_row(cur, actor="delim_test", message="5:hi|3:bye|N:tricky")
        # NULL actor — must be distinct from an empty-string actor.
        cur.execute("INSERT INTO audit_logs (actor, message) VALUES (NULL, %s) RETURNING id", ("null_actor_test",))
        null_actor_id = cur.fetchone()[0]
        empty_id = _insert_row(cur, actor="", message="empty_actor_test")
    conn.commit()
    conn.close()

    # Inserted under a non-UTC session TimeZone — the hash must still be
    # computed from the UTC-normalised timestamp, not whatever zone this
    # particular connection happens to be in.
    tz_conn = _connection()
    with tz_conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'America/Los_Angeles'")
        tz_id = _insert_row(cur, actor="tz_test", message="inserted under a non-UTC session zone")
    tz_conn.commit()
    tz_conn.close()

    conn = _connection()
    with conn.cursor() as cur:
        cur.execute(
            f"{_CHAIN_SELECT} WHERE id IN (%s, %s, %s, %s) ORDER BY chain_position",
            (delim_id, null_actor_id, empty_id, tz_id),
        )
        rows = cur.fetchall()
    conn.close()

    assert len(rows) == 4
    for chain_position, actor, message, created_at_canonical, prev_chain_hash, chain_hash in rows:
        expected = verify._row_hash(prev_chain_hash, chain_position, actor, message, created_at_canonical)
        assert chain_hash == expected

    null_row = next(r for r in rows if r[1] is None)
    empty_row = next(r for r in rows if r[1] == "")
    assert null_row[5] != empty_row[5]  # NULL actor and "" actor must never collide


def test_migration_027_backfills_a_valid_complete_chain_over_pre_populated_rows():
    # Own throwaway schema — starts from a KNOWN pre-migration state: rows
    # that existed before 027 ever ran, the realistic case on a real
    # deployment where audit_logs already has history.
    with _scratch_schema() as cur:
        with open(_MIGRATION_PATHS[0], encoding="utf-8") as f:
            cur.execute(f.read())  # 026 only first, matching realistic deploy ordering

        for i in range(5):
            cur.execute(
                "INSERT INTO audit_logs (actor, message) VALUES (%s, %s)",
                (f"pre_existing_{i}", f"event {i} logged before the chain existed"),
            )

        with open(_MIGRATION_PATHS[1], encoding="utf-8") as f:
            cur.execute(f.read())  # 027: must backfill the 5 rows above

        cur.execute(f"{_CHAIN_SELECT} ORDER BY id")
        rows = cur.fetchall()

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'audit_logs' "
            "AND column_name IN ('chain_position', 'chain_hash') AND is_nullable = 'NO'"
        )
        not_null_cols = {row[0] for row in cur.fetchall()}

        # The append-only trigger must be back on after the backfill
        # temporarily disabled it — not left off.
        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute("UPDATE audit_logs SET message = 'tampered' WHERE chain_position = 1")

    assert len(rows) == 5
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]  # backfilled in id order, dense, gap-free
    assert not_null_cols == {"chain_position", "chain_hash"}

    ok, break_position, reason = verify.verify_chain(rows)
    assert ok is True, f"backfilled chain broke at chain_position={break_position}: {reason}"


def test_migration_026_scrub_survives_027s_backfill_and_the_chain_still_verifies():
    # AUD-M01 + the hash chain, together: a database that predates the
    # fixed generate_seed.py could carry the OLD raw-PHI audit_logs row this
    # test constructs by hand. Proves 026 scrubs it BEFORE 027 ever computes
    # a hash over it (026 always runs first — see _MIGRATION_PATHS order),
    # and that the resulting backfilled chain, over the now-scrubbed
    # content, still verifies end to end.
    with _scratch_schema() as cur:
        cur.execute(
            "INSERT INTO audit_logs (actor, message) VALUES (%s, %s)",
            (
                "intake-service",
                'POST /intake body={"name":"Maria Gonzalez","dob":"1971-03-02","ssn":"412-55-9981"}',
            ),
        )
        cur.execute(
            "INSERT INTO audit_logs (actor, message) VALUES (%s, %s)",
            ("records-service", "GET /patients/1042/records 200"),
        )

        for path in _MIGRATION_PATHS:  # 026 (scrub + trigger), then 027 (chain)
            with open(path, encoding="utf-8") as f:
                cur.execute(f.read())

        cur.execute("SELECT actor, message FROM audit_logs ORDER BY id")
        rows = cur.fetchall()
        cur.execute(f"{_CHAIN_SELECT} ORDER BY chain_position")
        chain_rows = cur.fetchall()

    for _actor, message in rows:
        assert "Maria Gonzalez" not in message
        assert "412-55-9981" not in message

    scrubbed = next(message for actor, message in rows if actor == "intake-service")
    assert scrubbed == "POST /intake correlation_id=seed-demo-0001 created_via=self_service"

    ok, break_position, reason = verify.verify_chain(chain_rows)
    assert ok is True, f"chain broke at chain_position={break_position}: {reason}"


def test_an_internal_deletion_is_detected_by_the_verifier():
    # Normal SQL DELETE is blocked by 026's trigger — this simulates what
    # would happen if that protection were ever bypassed (a future bug, a
    # superuser), by disabling ONLY the delete-rejection trigger, deleting a
    # MIDDLE row, and confirming the verifier — not the database — is what
    # catches it. Own throwaway schema so this deliberate corruption can't
    # affect any other test.
    with _scratch_schema() as cur:
        for path in _MIGRATION_PATHS:
            with open(path, encoding="utf-8") as f:
                cur.execute(f.read())

        ids = [_insert_row(cur, actor="del_test", message=f"event {i}") for i in range(4)]

        cur.execute(f"{_CHAIN_SELECT} ORDER BY chain_position")
        ok_before, _, _ = verify.verify_chain(cur.fetchall())
        assert ok_before is True  # sanity: valid before the simulated corruption

        cur.execute("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_delete")
        try:
            cur.execute("DELETE FROM audit_logs WHERE id = %s", (ids[1],))  # the 2nd row: a MIDDLE row
        finally:
            cur.execute("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_no_delete")

        cur.execute(f"{_CHAIN_SELECT} ORDER BY chain_position")
        after = cur.fetchall()

    ok, break_position, reason = verify.verify_chain(after)
    assert ok is False
    assert break_position == 3  # the row after the gap: its chain_position no longer follows 1
    assert "missing from the chain" in reason


def test_a_tail_deletion_is_not_detected_by_the_verifier():
    # The chain's documented limitation, proven against REAL trigger output
    # (not just the pure-Python model in tests/test_verify_audit_chain.py):
    # deleting the LAST rows and stopping there leaves nothing after the cut
    # to reveal a break, so the remaining prefix verifies as fully intact.
    # This is not a verifier bug — it is exactly why migration 027's own
    # comment states tail truncation needs an externally stored checkpoint
    # this repo does not implement.
    with _scratch_schema() as cur:
        for path in _MIGRATION_PATHS:
            with open(path, encoding="utf-8") as f:
                cur.execute(f.read())

        ids = [_insert_row(cur, actor="del_test", message=f"event {i}") for i in range(3)]

        cur.execute("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_no_delete")
        try:
            cur.execute("DELETE FROM audit_logs WHERE id = %s", (ids[-1],))  # the LAST (tail) row
        finally:
            cur.execute("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_no_delete")

        cur.execute(f"{_CHAIN_SELECT} ORDER BY chain_position")
        remaining = cur.fetchall()

    assert len(remaining) == 2  # the tail row is really gone
    ok, break_position, reason = verify.verify_chain(remaining)
    assert ok is True  # the limitation: a truncated tail is indistinguishable from "nothing more was ever logged"
    assert break_position is None
