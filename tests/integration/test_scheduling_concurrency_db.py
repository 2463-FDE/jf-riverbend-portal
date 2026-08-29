"""Integration test — requires a real Postgres (`make up`, or the CI
concurrency job's disposable postgres-only container).

W10 Final Stage 2: re-proves the RIV-175 double-booking invariant (migration
013) directly against services/scheduling-service/book.py — no gateway, no
HTTP, no scheduling-service process — so it can run in a lightweight,
Postgres-only CI job alongside the other concurrency-defining invariants.
tests/integration/test_scheduling_concurrency.py (full stack, `make up`,
through the real gateway/HTTP path) remains the end-to-end proof; this file
is the DB-level one.

Uses its own throwaway slot row (never a shared seed slot) against the
seeded demo patient 1042, and removes it afterward — never touches any
other row.

Run with:  pytest -m integration tests/integration/test_scheduling_concurrency_db.py
"""
import concurrent.futures
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from conftest import load_module  # noqa: E402

pytestmark = pytest.mark.integration

os.environ.setdefault("DB_HOST", "localhost")

book = load_module("services/scheduling-service/book.py", "scheduling_concurrency_db_book")

DB_DSN_PARTS = dict(
    host=os.getenv("DB_HOST", "localhost"), port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "riverbend"), user=os.getenv("DB_USER", "riverbend_app"),
    password=os.getenv("DB_PASSWORD", "changeme"),
)

_PATIENT_ID = 1042  # seeded demo patient — this file never writes to it, only reads


@pytest.fixture
def open_slot():
    start = datetime.now(timezone.utc) + timedelta(days=30)
    end = start + timedelta(minutes=30)
    conn = psycopg2.connect(**DB_DSN_PARTS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO slots (location, start_at, end_at, status) "
                "VALUES ('Concurrency Test Clinic', %s, %s, 'open') RETURNING id",
                (start, end),
            )
            slot_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    yield slot_id

    conn = psycopg2.connect(**DB_DSN_PARTS)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM appointments WHERE slot_id = %s", (slot_id,))
            cur.execute("DELETE FROM slots WHERE id = %s", (slot_id,))
        conn.commit()
    finally:
        conn.close()


def test_concurrent_bookings_for_the_same_slot_confirm_exactly_once(open_slot, monkeypatch):
    monkeypatch.setattr(book, "get_conn", lambda: psycopg2.connect(**DB_DSN_PARTS))

    def _attempt(n):
        return book.book(_PATIENT_ID, open_slot, idempotency_key=f"race-{uuid.uuid4().hex}", reason=f"racer-{n}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_attempt, range(6)))

    winners = [r for r in results if r[0] is not None]
    assert len(winners) == 1, f"exactly one distinct-key racer must win the slot: {results}"

    conn = psycopg2.connect(**DB_DSN_PARTS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM appointments WHERE slot_id = %s AND status = 'confirmed'",
                (open_slot,),
            )
            confirmed_count = cur.fetchone()[0]
    finally:
        conn.close()
    assert confirmed_count == 1, "the database invariant must allow at most one confirmed appointment per slot"
