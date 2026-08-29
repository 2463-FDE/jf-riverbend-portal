"""Integration test — requires a real Postgres (`make up`, or the CI
concurrency job's disposable postgres-only container).

Review finding ROI-MIG-001: db/migrations/035_disclosures_roi_request_unique.sql
originally went straight to `CREATE UNIQUE INDEX`, which fails outright
against a database that already has more than one disclosure row sharing a
non-null roi_request_id (the exact state the old, unlocked fulfill_roi_request
race could have left behind before this migration ever ran). The migration
now remediates deterministically first: for each non-null roi_request_id,
the earliest row (by disclosed_at, then id) keeps it; every later row in the
same group has roi_request_id set to NULL — no row is deleted, no other
column is touched, and every disclosure record survives.

Connects directly to Postgres and runs the ACTUAL migration file against a
throwaway, uniquely named schema — never public/the shared demo database
(mirrors tests/integration/test_audit_logs_hash_chain.py's isolation
pattern). The teardown fixture refuses to drop anything unless the active
session is still pointed at that exact generated schema, so a bug here can
never reach a schema this test didn't create.

Run with:  pytest -m integration tests/integration/test_disclosures_roi_request_unique_migration.py
"""
import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.errors  # noqa: E402

pytestmark = pytest.mark.integration

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MIGRATION_PATH = os.path.join(_REPO_ROOT, "db", "migrations", "035_disclosures_roi_request_unique.sql")

# The pre-035 shape (schema.sql before this migration): no unique index yet.
_BASE_TABLE_SQL = """
CREATE TABLE disclosures (
    id                       SERIAL PRIMARY KEY,
    patient_id               INTEGER NOT NULL,
    roi_request_id           INTEGER,
    authorization_id         INTEGER,
    disclosed_to             TEXT,
    disclosed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    authorization_reference  TEXT,
    purpose                  TEXT
);
"""


def _admin_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_ADMIN_USER", "riverbend_admin"),
        password=os.environ["DB_ADMIN_PASSWORD"],
    )


def _active_schema(cur) -> str:
    cur.execute("SELECT current_schema()")
    return cur.fetchone()[0]


@pytest.fixture()
def schema_conn():
    """One admin connection, pinned via search_path to a freshly created,
    uniquely named, disposable schema — never public/the shared demo
    database. A fresh schema per test (not module-scoped) so each test's
    seeded rows/migration run against a clean, independent copy of the
    table. Dropped at teardown ONLY if the connection's active schema still
    matches the exact schema this fixture created — fails closed (raises)
    rather than risk ever dropping the wrong schema."""
    test_schema = f"disclosures_035_test_{uuid.uuid4().hex[:12]}"
    conn = _admin_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {test_schema}")
        cur.execute(f"SET search_path TO {test_schema}, public")

    yield conn

    with conn.cursor() as cur:
        active = _active_schema(cur)
        if active != test_schema:
            raise RuntimeError(
                f"refusing to drop schema — active schema is {active!r}, expected "
                f"{test_schema!r}; this must never drop anything but the schema "
                f"this test created."
            )
        cur.execute(f"DROP SCHEMA {test_schema} CASCADE")
    conn.close()


@pytest.fixture()
def seeded_duplicates(schema_conn):
    """The exact pre-035 state ROI-MIG-001 describes: two disclosure rows
    sharing one roi_request_id (the old race's real-world output), plus a
    third, unrelated row with its own distinct roi_request_id — proving the
    remediation is scoped per-group, not a blanket first-row-wins across the
    whole table."""
    with schema_conn.cursor() as cur:
        cur.execute(_BASE_TABLE_SQL)
        cur.execute(
            "INSERT INTO disclosures (patient_id, roi_request_id, disclosed_to, disclosed_at, purpose) "
            "VALUES (1, 100, 'Dr. Earliest', '2026-01-01T00:00:00Z', 'earliest') RETURNING id"
        )
        earliest_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO disclosures (patient_id, roi_request_id, disclosed_to, disclosed_at, purpose) "
            "VALUES (1, 100, 'Dr. Later', '2026-01-02T00:00:00Z', 'later-duplicate') RETURNING id"
        )
        later_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO disclosures (patient_id, roi_request_id, disclosed_to, disclosed_at, purpose) "
            "VALUES (1, 200, 'Dr. Control', '2026-01-01T00:00:00Z', 'unrelated-request') RETURNING id"
        )
        control_id = cur.fetchone()[0]
    return {"earliest_id": earliest_id, "later_id": later_id, "control_id": control_id}


def _run_migration(schema_conn):
    # search_path is already pinned to the throwaway schema by the
    # schema_conn fixture, for the life of this one connection.
    with open(_MIGRATION_PATH, encoding="utf-8") as f:
        sql = f.read()
    with schema_conn.cursor() as cur:
        cur.execute(sql)


def _row(schema_conn, row_id):
    with schema_conn.cursor() as cur:
        cur.execute(
            "SELECT id, patient_id, roi_request_id, authorization_id, disclosed_to, "
            "disclosed_at, authorization_reference, purpose FROM disclosures WHERE id = %s",
            (row_id,),
        )
        return cur.fetchone()


def test_migration_succeeds_against_pre_existing_duplicates(schema_conn, seeded_duplicates):
    _run_migration(schema_conn)  # must not raise


def test_the_earliest_row_keeps_its_roi_request_id(schema_conn, seeded_duplicates):
    _run_migration(schema_conn)

    row = _row(schema_conn, seeded_duplicates["earliest_id"])
    assert row[2] == 100  # roi_request_id


def test_the_later_duplicate_survives_but_is_unlinked(schema_conn, seeded_duplicates):
    before = _row(schema_conn, seeded_duplicates["later_id"])
    _run_migration(schema_conn)
    after = _row(schema_conn, seeded_duplicates["later_id"])

    assert after is not None, "the duplicate row must never be deleted"
    assert after[2] is None  # roi_request_id nulled
    # Every other column is untouched: patient_id, authorization_id,
    # disclosed_to, disclosed_at, authorization_reference, purpose.
    untouched_before = before[1], before[3], before[4], before[5], before[6], before[7]
    untouched_after = after[1], after[3], after[4], after[5], after[6], after[7]
    assert untouched_before == untouched_after


def test_an_unrelated_request_id_is_never_touched(schema_conn, seeded_duplicates):
    before = _row(schema_conn, seeded_duplicates["control_id"])
    _run_migration(schema_conn)
    after = _row(schema_conn, seeded_duplicates["control_id"])

    assert before == after


def test_the_unique_index_rejects_a_new_duplicate_after_migration(schema_conn, seeded_duplicates):
    _run_migration(schema_conn)

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with schema_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO disclosures (patient_id, roi_request_id, disclosed_to) "
                "VALUES (1, 200, 'Dr. Duplicate')"
            )


def test_reapplying_the_migration_is_idempotent(schema_conn, seeded_duplicates):
    _run_migration(schema_conn)
    first_pass = {
        seeded_duplicates["earliest_id"]: _row(schema_conn, seeded_duplicates["earliest_id"]),
        seeded_duplicates["later_id"]: _row(schema_conn, seeded_duplicates["later_id"]),
        seeded_duplicates["control_id"]: _row(schema_conn, seeded_duplicates["control_id"]),
    }

    _run_migration(schema_conn)  # must not raise the second time either

    second_pass = {
        seeded_duplicates["earliest_id"]: _row(schema_conn, seeded_duplicates["earliest_id"]),
        seeded_duplicates["later_id"]: _row(schema_conn, seeded_duplicates["later_id"]),
        seeded_duplicates["control_id"]: _row(schema_conn, seeded_duplicates["control_id"]),
    }
    assert first_pass == second_pass
