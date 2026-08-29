"""Integration test — requires a real Postgres (`make up`, or the CI
concurrency job's disposable postgres-only container).

W10 Final Stage 4: proves migration 036's actual triggers — not just the
application-level agent_lifecycle.py wrapper (see
tests/test_agent_lifecycle_durable_trace.py for that DB-less coverage) —
under a real database: sequence is assigned server-side (a client-supplied
value is silently overridden), concurrent inserts for the SAME
correlation_id serialize correctly via the transaction-scoped advisory
lock (no duplicate or out-of-order sequence numbers), and UPDATE/DELETE are
rejected outright, for both the runtime role and the table owner (mirrors
test_audit_logs_append_only.py's own proof for audit_logs).

Connects directly to Postgres, same pattern as
tests/integration/test_agent_draft_provenance_contract.py — every row this
file creates is a fresh, uuid-suffixed throwaway correlation_id, never
touching seeded/shared data.

Run with:  pytest -m integration tests/integration/test_agent_lifecycle_events_migration.py
"""
import os
import threading
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.errors  # noqa: E402

pytestmark = pytest.mark.integration

_DB_HOST = os.getenv("DB_HOST", "localhost")
_DB_PORT = os.getenv("DB_PORT", "5432")
_DB_NAME = os.getenv("DB_NAME", "riverbend")
_DB_USER = os.getenv("DB_USER", "riverbend_app")
_DB_PASSWORD = os.getenv("DB_PASSWORD", "changeme")
_DB_ADMIN_USER = os.getenv("DB_ADMIN_USER", "riverbend_admin")


def _connect(user=_DB_USER, password=_DB_PASSWORD):
    conn = psycopg2.connect(
        host=_DB_HOST, port=_DB_PORT, dbname=_DB_NAME, user=user, password=password,
    )
    conn.autocommit = False
    return conn


@pytest.fixture()
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def _correlation_id():
    return f"lifecycle-test-{uuid.uuid4().hex[:12]}"


def _insert(conn, correlation_id, stage, attributes_json="{}", client_sequence=999):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_lifecycle_events (correlation_id, sequence, stage, attributes) "
            "VALUES (%s, %s, %s, %s) RETURNING sequence",
            (correlation_id, client_sequence, stage, attributes_json),
        )
        return cur.fetchone()[0]


def test_sequence_is_assigned_server_side_ignoring_the_client_value(conn):
    correlation_id = _correlation_id()
    first = _insert(conn, correlation_id, "request", client_sequence=999)
    second = _insert(conn, correlation_id, "draft", client_sequence=999)
    conn.commit()

    assert (first, second) == (1, 2)


def test_two_concurrent_inserts_for_the_same_correlation_id_get_distinct_sequential_numbers():
    correlation_id = _correlation_id()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def _do_insert(stage):
        conn = _connect()
        try:
            barrier.wait(timeout=5)
            seq = _insert(conn, correlation_id, stage)
            conn.commit()
            results.append(seq)
        except Exception as exc:  # pragma: no cover - failure path asserted below
            errors.append(exc)
        finally:
            conn.close()

    t1 = threading.Thread(target=_do_insert, args=("request",))
    t2 = threading.Thread(target=_do_insert, args=("draft",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"concurrent insert raised: {errors}"
    assert sorted(results) == [1, 2], "both must succeed with distinct, sequential numbers"

    # cleanup: append-only means we cannot delete these rows even as admin —
    # they are fresh, uuid-suffixed throwaway ids, same convention as
    # test_agent_draft_provenance_contract.py's own uncommented rows.


def test_update_is_rejected_for_the_runtime_role(conn):
    correlation_id = _correlation_id()
    _insert(conn, correlation_id, "request")
    conn.commit()

    with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
        with conn.cursor() as cur:
            cur.execute("UPDATE agent_lifecycle_events SET stage = 'display' WHERE correlation_id = %s",
                        (correlation_id,))
    conn.rollback()


def test_delete_is_rejected_for_the_runtime_role(conn):
    correlation_id = _correlation_id()
    _insert(conn, correlation_id, "request")
    conn.commit()

    with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_lifecycle_events WHERE correlation_id = %s", (correlation_id,))
    conn.rollback()


def test_update_is_rejected_even_for_the_table_owner_admin_role():
    """Mirrors test_audit_logs_append_only.py's own proof: the trigger fires
    regardless of caller, including the admin role that owns the table —
    only an explicit ALTER TABLE ... DISABLE TRIGGER (a schema change, not
    an ordinary DML statement) could bypass it."""
    admin_password = os.environ.get("DB_ADMIN_PASSWORD")
    if not admin_password:
        pytest.skip("DB_ADMIN_PASSWORD not set")
    admin_conn = _connect(user=_DB_ADMIN_USER, password=admin_password)
    try:
        correlation_id = _correlation_id()
        _insert(admin_conn, correlation_id, "request")
        admin_conn.commit()

        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            with admin_conn.cursor() as cur:
                cur.execute("UPDATE agent_lifecycle_events SET stage = 'display' WHERE correlation_id = %s",
                            (correlation_id,))
        admin_conn.rollback()
    finally:
        admin_conn.close()


def test_an_unrecognized_stage_is_rejected_by_the_check_constraint(conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(conn, _correlation_id(), "not_a_real_stage")
    conn.rollback()
