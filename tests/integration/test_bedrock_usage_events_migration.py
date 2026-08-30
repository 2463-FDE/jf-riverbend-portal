"""Integration test — requires a real Postgres (`make up`). Proves
migration 037's actual invariants: idempotency_key uniqueness under
concurrency, append-only enforcement (runtime role and table owner), and
the rate_version/cost_usd CHECK constraint.
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
    conn = psycopg2.connect(host=_DB_HOST, port=_DB_PORT, dbname=_DB_NAME, user=user, password=password)
    conn.autocommit = False
    return conn


@pytest.fixture()
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def _idempotency_key():
    return f"usage-test-{uuid.uuid4().hex[:12]}:1"


def _insert(conn, idempotency_key, model_id="model-x", use_case="summary_agent_chat"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bedrock_usage_events (idempotency_key, provider, model_id, use_case, "
            "input_tokens, output_tokens) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (idempotency_key, "bedrock", model_id, use_case, 100, 20),
        )
        return cur.fetchone()[0]


def _run_pair(fn):
    threads = [threading.Thread(target=fn), threading.Thread(target=fn)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)


def test_concurrent_duplicate_idempotency_keys_yield_one_stored_row():
    """Make retries/double recording deterministic: two simultaneous
    inserts for the SAME idempotency_key must yield exactly one row."""
    key = _idempotency_key()
    barrier = threading.Barrier(2)
    outcomes = []

    def _do_insert():
        c = _connect()
        try:
            barrier.wait(timeout=5)
            try:
                row_id = _insert(c, key)
                c.commit()
                outcomes.append(("ok", row_id))
            except psycopg2.errors.UniqueViolation:
                c.rollback()
                outcomes.append(("conflict", None))
        finally:
            c.close()

    _run_pair(_do_insert)
    assert len(outcomes) == 2
    successes = [o for o in outcomes if o[0] == "ok"]
    conflicts = [o for o in outcomes if o[0] == "conflict"]
    assert len(successes) == 1, f"exactly one insert must win: {outcomes}"
    assert len(conflicts) == 1, f"the other must fail on the unique index: {outcomes}"


@pytest.mark.parametrize("sql", [
    "UPDATE bedrock_usage_events SET input_tokens = 0 WHERE idempotency_key = %s",
    "DELETE FROM bedrock_usage_events WHERE idempotency_key = %s",
])
def test_mutation_is_rejected_for_the_runtime_role(conn, sql):
    key = _idempotency_key()
    _insert(conn, key)
    conn.commit()
    with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
        with conn.cursor() as cur:
            cur.execute(sql, (key,))
    conn.rollback()


def test_update_is_rejected_even_for_the_table_owner_admin_role():
    admin_password = os.environ.get("DB_ADMIN_PASSWORD")
    if not admin_password:
        pytest.skip("DB_ADMIN_PASSWORD not set")
    admin_conn = _connect(user=_DB_ADMIN_USER, password=admin_password)
    try:
        key = _idempotency_key()
        _insert(admin_conn, key)
        admin_conn.commit()
        with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
            with admin_conn.cursor() as cur:
                cur.execute("UPDATE bedrock_usage_events SET input_tokens = 0 WHERE idempotency_key = %s", (key,))
        admin_conn.rollback()
    finally:
        admin_conn.close()


def test_an_unrecognized_use_case_is_rejected_by_the_check_constraint(conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(conn, _idempotency_key(), use_case="not_a_real_use_case")
    conn.rollback()


def test_cost_without_a_rate_version_is_rejected_by_the_check_constraint(conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bedrock_usage_events (idempotency_key, provider, model_id, use_case, cost_usd) "
                "VALUES (%s, 'bedrock', 'model-x', 'summary_agent_chat', 0.05)",
                (_idempotency_key(),),
            )
    conn.rollback()
