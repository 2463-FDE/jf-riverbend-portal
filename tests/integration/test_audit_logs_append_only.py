"""Integration test — requires a real Postgres (`make up`). P3 audit
integrity (w8-planner-2): migration 026 makes audit_logs append-only at the
database boundary via a BEFORE UPDATE/DELETE trigger, since a REVOKE would
have no effect (riverbend_app owns the table — see that migration for why;
PR #85 is the actual ownership-transfer fix). A tamper-evident hash chain
is separate, later work — see PR #86 and
tests/integration/test_audit_logs_hash_chain.py.

Most tests share one module-scoped isolated schema (`_isolated_schema`
below). The PHI-scrub tests need to observe migration 026 running against a
KNOWN, freshly constructed starting state — a pre-migration row carrying the
exact legacy content 026's own scrub targets — and use their own throwaway
schema instead so they can't affect, or be affected by, the shared module
fixture's tests. Neither ever touches the real `public.audit_logs` — mirrors
tests/integration/test_policy_corpus_pipeline.py's isolation pattern, for
the same reason: this table can carry real rows from other work against the
same shared local database, and deleting/mutating them to test a delete/
mutation-rejection contract would be exactly backwards.

Run with:  pytest -m integration tests/integration/test_audit_logs_append_only.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import contextlib
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MIGRATION_026 = os.path.join(_REPO_ROOT, "db", "migrations", "026_audit_logs_append_only.sql")
_TEST_SCHEMA = f"audit_logs_test_{uuid.uuid4().hex[:12]}"

# The pre-026 shape (001_init.sql / schema.sql before this migration): the
# realistic starting point migration 026 actually runs against on a real
# deployment — an existing table, already carrying the deleted_at column
# this migration must drop.
_BASE_TABLE_SQL = """
CREATE TABLE audit_logs (
    id          SERIAL PRIMARY KEY,
    actor       TEXT,
    message     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);
"""


def _bare_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_USER", "riverbend_app"),
        password=os.getenv("DB_PASSWORD", "changeme"),
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
    setup_conn = _bare_connection()
    setup_conn.autocommit = True
    with setup_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {_TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
        cur.execute(_BASE_TABLE_SQL)
        with open(_MIGRATION_026, encoding="utf-8") as f:
            cur.execute(f.read())
    setup_conn.close()

    yield

    teardown_conn = _bare_connection()
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
    for tests that need a KNOWN starting state (a pre-migration row) without
    affecting or being affected by the shared fixture's tests. Yields a
    cursor already pointed at it via search_path, with the pre-026 base
    table already created; the caller applies 026 itself."""
    schema = f"audit_logs_scratch_{uuid.uuid4().hex[:10]}"
    conn = _bare_connection()
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


def test_insert_still_works():
    conn = _connection()
    with conn.cursor() as cur:
        row_id = _insert_row(cur)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT actor, message FROM audit_logs WHERE id = %s", (row_id,))
        actor, message = cur.fetchone()
    conn.close()

    assert (actor, message) == ("tester", "test event")


def test_update_is_rejected():
    conn = _connection()
    with conn.cursor() as cur:
        row_id = _insert_row(cur)
    conn.commit()

    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute("UPDATE audit_logs SET message = 'tampered' WHERE id = %s", (row_id,))
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT message FROM audit_logs WHERE id = %s", (row_id,))
        (message,) = cur.fetchone()
    conn.close()

    assert message == "test event"


def test_delete_is_rejected():
    conn = _connection()
    with conn.cursor() as cur:
        row_id = _insert_row(cur)
    conn.commit()

    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            cur.execute("DELETE FROM audit_logs WHERE id = %s", (row_id,))
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_logs WHERE id = %s", (row_id,))
        (count,) = cur.fetchone()
    conn.close()

    assert count == 1


def test_deleted_at_column_no_longer_exists():
    conn = _connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'audit_logs'",
            (_TEST_SCHEMA,),
        )
        columns = {row[0] for row in cur.fetchall()}
    conn.close()

    assert "deleted_at" not in columns
    assert columns == {"id", "actor", "message", "created_at"}


def test_the_migration_is_safe_to_reapply():
    # apply.sh's own contract: every migration must be a no-op the second
    # time, safe to run against a database at any prior migration point —
    # including one that already has rows, which is the realistic case by
    # this point in the module (earlier tests have already inserted).
    conn = _bare_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {_TEST_SCHEMA}, public")
        with open(_MIGRATION_026, encoding="utf-8") as f:
            cur.execute(f.read())  # must not raise
    conn.close()


# --- AUD-M01: the raw-PHI legacy row scrub -----------------------------------


def test_migration_026_scrubs_the_known_legacy_phi_row():
    # AUD-M01 (code review): a database that predates the fixed
    # generate_seed.py could carry the OLD raw-PHI audit_logs row this test
    # constructs by hand. Own throwaway schema, starting from that exact
    # pre-migration content — proves 026 scrubs it (a targeted, idempotent
    # UPDATE matched by content, not by id) as part of its own run.
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

        with open(_MIGRATION_026, encoding="utf-8") as f:
            cur.execute(f.read())

        cur.execute("SELECT actor, message FROM audit_logs ORDER BY id")
        rows = cur.fetchall()

    for _actor, message in rows:
        assert "Maria Gonzalez" not in message
        assert "412-55-9981" not in message
        assert '"dob"' not in message
        assert '"ssn"' not in message
        assert "body={" not in message

    scrubbed = next(message for actor, message in rows if actor == "intake-service")
    assert scrubbed == "POST /intake correlation_id=seed-demo-0001 created_via=self_service"


def test_migration_026_scrub_is_a_no_op_on_already_scrubbed_content():
    # Idempotency: a database seeded from the CORRECTED generate_seed.py (or
    # one that already had 026 applied) has nothing matching the legacy
    # WHERE clause — reapplying must not raise and must not touch anything.
    with _scratch_schema() as cur:
        cur.execute(
            "INSERT INTO audit_logs (actor, message) VALUES (%s, %s)",
            ("intake-service", "POST /intake correlation_id=seed-demo-0001 created_via=self_service"),
        )
        with open(_MIGRATION_026, encoding="utf-8") as f:
            cur.execute(f.read())

        cur.execute("SELECT message FROM audit_logs WHERE actor = 'intake-service'")
        (message,) = cur.fetchone()

    assert message == "POST /intake correlation_id=seed-demo-0001 created_via=self_service"
