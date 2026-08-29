"""Integration test — requires a real Postgres (`make up`). Proves
migration 036's actual triggers: sequence assignment, advisory-lock
serialization, append-only enforcement (runtime role and table owner),
and the display partial-unique-index invariant."""
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


def _run_pair(fn):
    threads = [threading.Thread(target=fn), threading.Thread(target=fn)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)


def test_sequence_is_assigned_server_side_ignoring_the_client_value(conn):
    correlation_id = _correlation_id()
    first = _insert(conn, correlation_id, "request", client_sequence=999)
    second = _insert(conn, correlation_id, "draft", client_sequence=999)
    conn.commit()
    assert (first, second) == (1, 2)


def test_two_concurrent_inserts_for_the_same_correlation_id_get_distinct_sequential_numbers():
    correlation_id = _correlation_id()
    barrier = threading.Barrier(2)
    results, errors, stages = [], [], iter(["request", "draft"])

    def _do_insert():
        conn = _connect()
        try:
            barrier.wait(timeout=5)
            seq = _insert(conn, correlation_id, next(stages))
            conn.commit()
            results.append(seq)
        except Exception as exc:  # pragma: no cover - failure path asserted below
            errors.append(exc)
        finally:
            conn.close()

    _run_pair(_do_insert)
    assert not errors, f"concurrent insert raised: {errors}"
    assert sorted(results) == [1, 2], "both must succeed with distinct, sequential numbers"


def test_concurrent_duplicate_display_appends_yield_one_stored_display(conn):
    """ALC-DISPLAY-REPEAT: two simultaneous displays yield one stored row."""
    correlation_id = _correlation_id()
    _insert(conn, correlation_id, "request")
    conn.commit()
    barrier = threading.Barrier(2)
    outcomes = []

    def _do_display_insert():
        c = _connect()
        try:
            barrier.wait(timeout=5)
            try:
                seq = _insert(c, correlation_id, "display")
                c.commit()
                outcomes.append(("ok", seq))
            except psycopg2.errors.UniqueViolation:
                c.rollback()
                outcomes.append(("conflict", None))
        finally:
            c.close()

    _run_pair(_do_display_insert)
    assert len(outcomes) == 2
    successes = [o for o in outcomes if o[0] == "ok"]
    conflicts = [o for o in outcomes if o[0] == "conflict"]
    assert len(successes) == 1, f"exactly one display insert must win: {outcomes}"
    assert len(conflicts) == 1, f"the other must fail on the partial unique index: {outcomes}"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sequence, stage FROM agent_lifecycle_events "
            "WHERE correlation_id = %s ORDER BY sequence",
            (correlation_id,),
        )
        rows = cur.fetchall()
    stages = [stage for _, stage in rows]
    sequences = [seq for seq, _ in rows]
    assert stages.count("display") == 1
    assert sequences == sorted(sequences) == sorted(set(sequences)), (
        "sequence must stay strictly increasing with no duplicates, even though "
        "one insert attempt failed"
    )


@pytest.mark.parametrize("sql", [
    "UPDATE agent_lifecycle_events SET stage = 'display' WHERE correlation_id = %s",
    "DELETE FROM agent_lifecycle_events WHERE correlation_id = %s",
])
def test_mutation_is_rejected_for_the_runtime_role(conn, sql):
    correlation_id = _correlation_id()
    _insert(conn, correlation_id, "request")
    conn.commit()
    with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
        with conn.cursor() as cur:
            cur.execute(sql, (correlation_id,))
    conn.rollback()


def test_update_is_rejected_even_for_the_table_owner_admin_role():
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
