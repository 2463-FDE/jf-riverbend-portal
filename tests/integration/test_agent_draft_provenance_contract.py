"""Integration tests for migration 020's agent_draft_provenance /
agent_draft_citation contract (PR #53) — automated coverage of what was
verified by hand, against a live database, during that PR's review.

Connects to Postgres directly (same pattern as
tests/integration/test_pgvector_retrieval.py) — this deliverable adds no HTTP
route; the whole contract lives in the schema's CHECK constraints, triggers
and indexes, not in application code.

Requires the full stack up (`make up`) on a fresh volume so migration 020's
constraints/triggers are actually present.

Run with:  pytest -m integration tests/integration/test_agent_draft_provenance_contract.py
Skipped by default in CI (`pytest -m "not integration"`).
"""
import concurrent.futures
import os
import threading
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.errors  # noqa: E402

pytestmark = pytest.mark.integration

_DB_HOST = os.getenv("DB_HOST", "localhost")  # this test runs on the host, not in-network
_DB_PORT = os.getenv("DB_PORT", "5432")
_DB_NAME = os.getenv("DB_NAME", "riverbend")
_DB_USER = os.getenv("DB_USER", "riverbend_app")
_DB_PASSWORD = os.getenv("DB_PASSWORD", "changeme")


def _connect():
    conn = psycopg2.connect(
        host=_DB_HOST, port=_DB_PORT, dbname=_DB_NAME, user=_DB_USER, password=_DB_PASSWORD,
    )
    conn.autocommit = False
    return conn


@pytest.fixture()
def conn():
    c = _connect()
    yield c
    c.rollback()  # never leave a failed/partial transaction open on the shared connection
    c.close()


@pytest.fixture()
def patient_id(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO patients (name, dob, created_via) VALUES (%s, %s, %s) RETURNING id",
            (f"Contract Test Patient {uuid.uuid4().hex[:8]}", "1990-01-01", "front_desk"),
        )
        pid = cur.fetchone()[0]
    conn.commit()
    return pid


@pytest.fixture()
def user_id(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
            (f"contract_test_{uuid.uuid4().hex[:8]}", "x", "staff"),
        )
        uid = cur.fetchone()[0]
    conn.commit()
    return uid


def _insert_draft(conn, *, patient_id, version, status="draft", provenance_label="real",
                   correlation_id="corr-contract", model_id="model-x", prompt_version="v1",
                   validation_code=None, generated_text="draft text",
                   reviewed_by=None, approved_at=None, rejected_at=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_draft_provenance
                (patient_id, version, status, provenance_label, correlation_id,
                 model_id, prompt_version, validation_code, generated_text,
                 reviewed_by, approved_at, rejected_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (patient_id, version, status, provenance_label, correlation_id,
             model_id, prompt_version, validation_code, generated_text,
             reviewed_by, approved_at, rejected_at),
        )
        draft_id = cur.fetchone()[0]
    conn.commit()
    return draft_id


def _set_status(conn, draft_id, **fields):
    sets = ", ".join(f"{k} = %s" for k in fields)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE agent_draft_provenance SET {sets} WHERE id = %s",
            (*fields.values(), draft_id),
        )
    conn.commit()


def _insert_citation(conn, draft_id, citation_id="c1", source_id="doc-1"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_draft_citation (draft_id, source_id, source_version, citation_id) "
            "VALUES (%s, %s, %s, %s)",
            (draft_id, source_id, "v1", citation_id),
        )
    conn.commit()


# --- transitions -------------------------------------------------------- #


def test_the_full_valid_chain_succeeds(conn, patient_id, user_id):
    """draft -> validated -> approved -> superseded, exactly as the client's
    review workflow requires."""
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)

    _set_status(conn, draft_id, status="validated", validation_code="PASS")
    _set_status(conn, draft_id, status="approved", reviewed_by=user_id, approved_at="now()")
    _set_status(conn, draft_id, status="superseded")

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agent_draft_provenance WHERE id = %s", (draft_id,))
        assert cur.fetchone()[0] == "superseded"


def test_a_refusal_is_a_valid_terminal_transition(conn, patient_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _set_status(conn, draft_id, status="refused", validation_code="UNSUPPORTED_CLAIM")

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agent_draft_provenance WHERE id = %s", (draft_id,))
        assert cur.fetchone()[0] == "refused"


def test_a_rejection_is_a_valid_terminal_transition(conn, patient_id, user_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _set_status(conn, draft_id, status="validated", validation_code="PASS")
    _set_status(conn, draft_id, status="rejected", reviewed_by=user_id, rejected_at="now()")

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM agent_draft_provenance WHERE id = %s", (draft_id,))
        assert cur.fetchone()[0] == "rejected"


@pytest.mark.parametrize("skip_to", ["approved", "rejected", "superseded"])
def test_skipping_straight_from_draft_is_rejected(conn, patient_id, skip_to):
    """draft can only ever move to validated or refused directly — every
    other status requires passing through one of those first."""
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_draft_provenance SET status = %s WHERE id = %s",
                (skip_to, draft_id),
            )
    conn.rollback()


@pytest.mark.parametrize("terminal_status", ["refused", "rejected", "superseded"])
def test_a_terminal_status_can_never_move_again(conn, patient_id, user_id, terminal_status):
    """refused/rejected/superseded are dead ends — no further transition is
    ever valid, including back to a status the row already held."""
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    if terminal_status == "refused":
        _set_status(conn, draft_id, status="refused", validation_code="UNSUPPORTED_CLAIM")
    elif terminal_status == "rejected":
        _set_status(conn, draft_id, status="validated", validation_code="PASS")
        _set_status(conn, draft_id, status="rejected", reviewed_by=user_id, rejected_at="now()")
    else:  # superseded
        _set_status(conn, draft_id, status="validated", validation_code="PASS")
        _set_status(conn, draft_id, status="approved", reviewed_by=user_id, approved_at="now()")
        _set_status(conn, draft_id, status="superseded")

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_draft_provenance SET status = 'validated' WHERE id = %s",
                (draft_id,),
            )
    conn.rollback()


# --- approval uniqueness ------------------------------------------------- #


def test_only_one_approved_version_per_patient_at_a_time(conn, patient_id, user_id):
    d1 = _insert_draft(conn, patient_id=patient_id, version=1)
    d2 = _insert_draft(conn, patient_id=patient_id, version=2)
    _set_status(conn, d1, status="validated", validation_code="PASS")
    _set_status(conn, d2, status="validated", validation_code="PASS")
    _set_status(conn, d1, status="approved", reviewed_by=user_id, approved_at="now()")

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_draft_provenance SET status = 'approved', reviewed_by = %s, "
                "approved_at = now() WHERE id = %s",
                (user_id, d2),
            )
    conn.rollback()


def test_two_different_patients_may_each_have_an_approved_version(conn, patient_id, user_id):
    """The partial unique index is scoped to (patient_id), not global."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO patients (name, dob, created_via) VALUES (%s, %s, %s) RETURNING id",
            (f"Second Patient {uuid.uuid4().hex[:8]}", "1991-01-01", "front_desk"),
        )
        other_patient_id = cur.fetchone()[0]
    conn.commit()

    d1 = _insert_draft(conn, patient_id=patient_id, version=1)
    d2 = _insert_draft(conn, patient_id=other_patient_id, version=1)
    for d in (d1, d2):
        _set_status(conn, d, status="validated", validation_code="PASS")
        _set_status(conn, d, status="approved", reviewed_by=user_id, approved_at="now()")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM agent_draft_provenance WHERE id = ANY(%s)", ([d1, d2],)
        )
        assert [row[0] for row in cur.fetchall()] == ["approved", "approved"]


# --- supersession --------------------------------------------------------- #


def test_supersede_then_approve_the_next_version_succeeds(conn, patient_id, user_id):
    """The intended regeneration sequence: retire the old approved version in
    the SAME kind of operation that lets a new one take its place."""
    d1 = _insert_draft(conn, patient_id=patient_id, version=1)
    d2 = _insert_draft(conn, patient_id=patient_id, version=2)
    for d in (d1, d2):
        _set_status(conn, d, status="validated", validation_code="PASS")
    _set_status(conn, d1, status="approved", reviewed_by=user_id, approved_at="now()")

    _set_status(conn, d1, status="superseded")
    _set_status(conn, d2, status="approved", reviewed_by=user_id, approved_at="now()")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM agent_draft_provenance WHERE id = ANY(%s) ORDER BY id",
            ([d1, d2],),
        )
        rows = dict(cur.fetchall())
    assert rows[d1] == "superseded"
    assert rows[d2] == "approved"


def test_a_superseded_version_retains_its_original_approval_record(conn, patient_id, user_id):
    """Superseding is not un-approving — the decider and timestamp from the
    original approval must survive, per agent_draft_decision_complete."""
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _set_status(conn, draft_id, status="validated", validation_code="PASS")
    _set_status(conn, draft_id, status="approved", reviewed_by=user_id, approved_at="now()")
    _set_status(conn, draft_id, status="superseded")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, reviewed_by, approved_at IS NOT NULL, rejected_at IS NULL "
            "FROM agent_draft_provenance WHERE id = %s",
            (draft_id,),
        )
        status, reviewed_by, has_approved_at, rejected_is_null = cur.fetchone()
    assert status == "superseded"
    assert reviewed_by == user_id
    assert has_approved_at is True
    assert rejected_is_null is True


# --- citation immutability ------------------------------------------------ #


def test_a_citation_update_is_always_forbidden(conn, patient_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _insert_citation(conn, draft_id)

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_draft_citation SET category = 'lab' WHERE draft_id = %s",
                (draft_id,),
            )
    conn.rollback()


def test_a_citation_may_be_added_and_removed_while_still_draft(conn, patient_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _insert_citation(conn, draft_id)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_draft_citation WHERE draft_id = %s", (draft_id,))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_draft_citation WHERE draft_id = %s", (draft_id,))
        assert cur.fetchone()[0] == 0


def test_a_citation_cannot_be_added_once_validated(conn, patient_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _set_status(conn, draft_id, status="validated", validation_code="PASS")

    with pytest.raises(psycopg2.Error):
        _insert_citation(conn, draft_id)
    conn.rollback()


def test_a_citation_cannot_be_removed_once_validated(conn, patient_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _insert_citation(conn, draft_id)
    _set_status(conn, draft_id, status="validated", validation_code="PASS")

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_draft_citation WHERE draft_id = %s", (draft_id,))
    conn.rollback()


# --- deletion protection --------------------------------------------------- #


def test_a_draft_status_row_with_citations_can_be_deleted_and_cascades(conn, patient_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _insert_citation(conn, draft_id)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_draft_provenance WHERE id = %s", (draft_id,))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_draft_provenance WHERE id = %s", (draft_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM agent_draft_citation WHERE draft_id = %s", (draft_id,))
        assert cur.fetchone()[0] == 0


def test_a_validated_row_with_citations_cannot_be_deleted(conn, patient_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _insert_citation(conn, draft_id)
    _set_status(conn, draft_id, status="validated", validation_code="PASS")

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_draft_provenance WHERE id = %s", (draft_id,))
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_draft_provenance WHERE id = %s", (draft_id,))
        assert cur.fetchone()[0] == 1


def test_a_validated_row_with_zero_citations_still_cannot_be_deleted(conn, patient_id):
    """THE bug PR #53 closed: with only the citation-cascade guard, a decided
    draft with no citations had no deletion protection at all, because
    nothing ever cascaded to trip the guard. The parent's own BEFORE DELETE
    trigger must refuse this directly, independent of citation count."""
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _set_status(conn, draft_id, status="validated", validation_code="PASS")

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_draft_provenance WHERE id = %s", (draft_id,))
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_draft_provenance WHERE id = %s", (draft_id,))
        assert cur.fetchone()[0] == 1


def test_an_approved_row_cannot_be_deleted(conn, patient_id, user_id):
    draft_id = _insert_draft(conn, patient_id=patient_id, version=1)
    _set_status(conn, draft_id, status="validated", validation_code="PASS")
    _set_status(conn, draft_id, status="approved", reviewed_by=user_id, approved_at="now()")

    with pytest.raises(psycopg2.Error):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_draft_provenance WHERE id = %s", (draft_id,))
    conn.rollback()


# --- concurrency-sensitive behavior ---------------------------------------- #
#
# These prove the `FOR SHARE` fix actually serializes a citation write against
# a concurrent status transition on the same draft, in both possible
# orderings — not by inspecting post-hoc state (which cannot distinguish
# "correctly serialized" from "got lucky this run"), but by proving the
# second operation genuinely BLOCKS for the first's full held-lock duration.


def _time_it(fn):
    started = time.monotonic()
    fn()
    return time.monotonic() - started


def test_a_concurrent_status_transition_blocks_on_an_open_citation_insert(patient_id):
    """Session A inserts a citation (opening the FOR SHARE lock on the still-
    draft parent) and holds its transaction open; Session B's concurrent
    transition to 'validated' must wait for A to finish, then correctly
    succeed (the citation was legitimately added before the transition)."""
    setup_conn = _connect()
    draft_id = _insert_draft(setup_conn, patient_id=patient_id, version=1)
    setup_conn.close()

    hold_seconds = 2.0
    a_holds_lock = threading.Event()

    def session_a():
        conn_a = _connect()
        try:
            with conn_a.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_draft_citation (draft_id, source_id, source_version, citation_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (draft_id, "doc-1", "v1", "c1"),
                )
            a_holds_lock.set()
            with conn_a.cursor() as cur:
                cur.execute("SELECT pg_sleep(%s)", (hold_seconds,))
            conn_a.commit()
        finally:
            conn_a.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(session_a)
        assert a_holds_lock.wait(timeout=5), "session A never reached its held lock"

        def session_b():
            conn_b = _connect()
            try:
                with conn_b.cursor() as cur:
                    cur.execute(
                        "UPDATE agent_draft_provenance SET status = 'validated', "
                        "validation_code = 'PASS' WHERE id = %s",
                        (draft_id,),
                    )
                conn_b.commit()
            finally:
                conn_b.close()

        elapsed = _time_it(lambda: pool.submit(session_b).result(timeout=10))
        future_a.result(timeout=10)

    assert elapsed >= hold_seconds * 0.7, (
        f"session B's UPDATE returned after only {elapsed:.2f}s — it should have "
        f"blocked for close to session A's {hold_seconds}s held FOR SHARE lock"
    )

    check_conn = _connect()
    with check_conn.cursor() as cur:
        cur.execute("SELECT status FROM agent_draft_provenance WHERE id = %s", (draft_id,))
        assert cur.fetchone()[0] == "validated"
        cur.execute("SELECT count(*) FROM agent_draft_citation WHERE draft_id = %s", (draft_id,))
        assert cur.fetchone()[0] == 1
    check_conn.close()


def test_a_concurrent_citation_insert_blocks_on_an_open_status_transition(patient_id):
    """The reverse ordering: Session A holds an open transition to
    'validated'; Session B's concurrent citation insert must wait, then
    correctly FAIL — the parent is no longer 'draft' by the time B's insert
    actually runs, and the citation must never be recorded."""
    setup_conn = _connect()
    draft_id = _insert_draft(setup_conn, patient_id=patient_id, version=1)
    setup_conn.close()

    hold_seconds = 2.0
    a_holds_lock = threading.Event()

    def session_a():
        conn_a = _connect()
        try:
            with conn_a.cursor() as cur:
                cur.execute(
                    "UPDATE agent_draft_provenance SET status = 'validated', "
                    "validation_code = 'PASS' WHERE id = %s",
                    (draft_id,),
                )
            a_holds_lock.set()
            with conn_a.cursor() as cur:
                cur.execute("SELECT pg_sleep(%s)", (hold_seconds,))
            conn_a.commit()
        finally:
            conn_a.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(session_a)
        assert a_holds_lock.wait(timeout=5), "session A never reached its held lock"

        b_error = {}

        def session_b():
            conn_b = _connect()
            try:
                with conn_b.cursor() as cur:
                    cur.execute(
                        "INSERT INTO agent_draft_citation "
                        "(draft_id, source_id, source_version, citation_id) "
                        "VALUES (%s, %s, %s, %s)",
                        (draft_id, "doc-1", "v1", "c1"),
                    )
                conn_b.commit()
            except psycopg2.Error as exc:
                b_error["type"] = type(exc).__name__
                conn_b.rollback()
            finally:
                conn_b.close()

        elapsed = _time_it(lambda: pool.submit(session_b).result(timeout=10))
        future_a.result(timeout=10)

    assert elapsed >= hold_seconds * 0.7, (
        f"session B's INSERT returned after only {elapsed:.2f}s — it should have "
        f"blocked for close to session A's {hold_seconds}s held row lock"
    )
    assert b_error, "session B's citation insert should have failed once the parent left 'draft'"

    check_conn = _connect()
    with check_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_draft_citation WHERE draft_id = %s", (draft_id,))
        assert cur.fetchone()[0] == 0
    check_conn.close()
