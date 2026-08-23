"""Integration tests — require the full stack up (`make up`) on localhost.

w9-fixes P0 4.2/4.3 (2026-08-23): before this fix, `slots.status` (the read
model GET /slots relies on) and `appointments` (the write model a booking
actually creates) could disagree indefinitely — booking never updated the
slot it just confirmed, so a slot stayed "open" forever regardless of having
a confirmed appointment sitting on it, and cancellation never reopened one
either. Separately, a booking's provider/location/time used to be whatever
the CALLER sent rather than derived from the slot actually being booked, so
a successful booking's own round-trip could disagree with the slot the
caller picked.

Both are now fixed at the source: services/scheduling-service/book.py locks
the slot row (`SELECT ... FOR UPDATE`), derives provider/location/
scheduled_for from it, and flips it to 'booked' in the SAME transaction as
the appointment insert; app.py's `cancel_appointment` reopens it on genuine
cancellation; and `list_slots` excludes any slot with a confirmed
appointment regardless of its stored status column, so pre-existing drift
(a legacy seed inconsistency — see db/seed/generate_seed.py's history, not
test pollution) fails closed instead of being offered again.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).
"""
import os
import subprocess
import uuid
from datetime import datetime, timedelta

import pytest

httpx = pytest.importorskip("httpx")
psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")
DB_DSN = os.getenv(
    "DATABASE_URL", "postgresql://riverbend_app:riverbend_app_pw@localhost:5432/riverbend"
)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The dedicated demo-booking pool — see db/seed/generate_seed.py's
# DEMO_SLOT_IDS and db/seed/demo_reset.sql's reset of this exact range.
_DEMO_SLOT_LO, _DEMO_SLOT_HI = 95001, 95016

# frontdesk's actual active grant — see test_scheduling_concurrency.py's own
# comment on why this can't be an arbitrary canonical patient id.
_GRANTED_PATIENT = 1737


def _token() -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": "frontdesk", "password": "portal123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _create_open_slot(days_ahead: int = 3, provider_id: int = 2, location: str = "Riverbend Main") -> tuple[int, datetime, datetime]:
    start = datetime.utcnow() + timedelta(days=days_ahead)
    end = start + timedelta(minutes=30)
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slots (provider_id, location, start_at, end_at, status) "
            "VALUES (%s, %s, %s, %s, 'open') RETURNING id",
            (provider_id, location, start, end),
        )
        slot_id = cur.fetchone()[0]
        conn.commit()
        return slot_id, start, end


@pytest.fixture
def cleanup():
    created = {"slot_ids": [], "appointment_ids": []}
    yield created
    if not created["slot_ids"] and not created["appointment_ids"]:
        return
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        if created["appointment_ids"]:
            cur.execute("DELETE FROM appointments WHERE id = ANY(%s)", (created["appointment_ids"],))
        if created["slot_ids"]:
            cur.execute("DELETE FROM slots WHERE id = ANY(%s)", (created["slot_ids"],))
        conn.commit()


def _open_slot_ids(headers) -> set:
    r = httpx.get(f"{GATEWAY}/slots", params={"limit": 200}, headers=headers, timeout=10)
    r.raise_for_status()
    return {s["id"] for s in r.json()["items"]}


def test_booking_an_offered_slot_removes_it_and_a_losing_competitor_gets_409(cleanup):
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id, _, _ = _create_open_slot()
    cleanup["slot_ids"].append(slot_id)

    assert slot_id in _open_slot_ids(headers)

    winner = httpx.post(
        f"{GATEWAY}/appointments",
        json={
            "patient_id": _GRANTED_PATIENT,
            "slot_id": slot_id,
            "idempotency_key": str(uuid.uuid4()),
            "reason": "availability test",
        },
        headers=headers,
        timeout=10,
    )
    assert winner.status_code == 201, winner.text
    cleanup["appointment_ids"].append(winner.json()["appointment_id"])

    assert slot_id not in _open_slot_ids(headers)

    loser = httpx.post(
        f"{GATEWAY}/appointments",
        json={
            "patient_id": _GRANTED_PATIENT,
            "slot_id": slot_id,
            "idempotency_key": str(uuid.uuid4()),
            "reason": "availability test — competing booking",
        },
        headers=headers,
        timeout=10,
    )
    assert loser.status_code == 409
    assert loser.json()["detail"]["error"] == "slot_taken"

    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM appointments WHERE slot_id = %s AND status = 'confirmed'", (slot_id,))
        assert cur.fetchone()[0] == 1


def test_a_booked_appointment_preserves_the_exact_provider_location_and_time_from_its_slot(cleanup):
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id, start, _ = _create_open_slot(provider_id=4, location="Riverbend Main")
    cleanup["slot_ids"].append(slot_id)

    booked = httpx.post(
        f"{GATEWAY}/appointments",
        json={
            "patient_id": _GRANTED_PATIENT,
            "slot_id": slot_id,
            "idempotency_key": str(uuid.uuid4()),
            "reason": "vaccination",
        },
        headers=headers,
        timeout=10,
    )
    assert booked.status_code == 201, booked.text
    appointment_id = booked.json()["appointment_id"]
    cleanup["appointment_ids"].append(appointment_id)

    listing = httpx.get(
        f"{GATEWAY}/appointments", params={"patient_id": _GRANTED_PATIENT}, headers=headers, timeout=10
    )
    assert listing.status_code == 200
    item = next(a for a in listing.json()["items"] if a["id"] == appointment_id)

    assert item["reason"] == "vaccination"
    assert item["provider"] == "Dr. Omar Haddad"
    assert item["location"] == "Riverbend Main"
    assert item["scheduled_for"].replace("Z", "+00:00").startswith(start.strftime("%Y-%m-%dT%H:%M"))
    assert item["status"] == "confirmed"


def test_cancelling_a_confirmed_appointment_reopens_its_slot(cleanup):
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id, _, _ = _create_open_slot()
    cleanup["slot_ids"].append(slot_id)

    booked = httpx.post(
        f"{GATEWAY}/appointments",
        json={
            "patient_id": _GRANTED_PATIENT,
            "slot_id": slot_id,
            "idempotency_key": str(uuid.uuid4()),
            "reason": "availability test",
        },
        headers=headers,
        timeout=10,
    )
    assert booked.status_code == 201, booked.text
    appointment_id = booked.json()["appointment_id"]
    cleanup["appointment_ids"].append(appointment_id)
    assert slot_id not in _open_slot_ids(headers)

    cancelled = httpx.post(f"{GATEWAY}/appointments/{appointment_id}/cancel", headers=headers, timeout=10)
    assert cancelled.status_code == 200, cancelled.text

    assert slot_id in _open_slot_ids(headers)


def test_a_slot_with_a_confirmed_appointment_stays_hidden_even_when_its_status_column_says_open(cleanup):
    """Reproduces the exact legacy contradiction 4.2 fixes: a slot whose own
    status column still says 'open' while a confirmed appointment already
    occupies it. GET /slots must fail closed regardless."""
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id, _, _ = _create_open_slot()
    cleanup["slot_ids"].append(slot_id)

    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO appointments (patient_id, slot_id, provider, reason, location, "
            "scheduled_for, status, idempotency_key) "
            "VALUES (%s, %s, 'Dr. Test', 'contradiction fixture', 'Riverbend Main', now(), "
            "'confirmed', %s) RETURNING id",
            (_GRANTED_PATIENT, slot_id, str(uuid.uuid4())),
        )
        appointment_id = cur.fetchone()[0]
        conn.commit()
    cleanup["appointment_ids"].append(appointment_id)

    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM slots WHERE id = %s", (slot_id,))
        assert cur.fetchone()[0] == "open"  # the contradiction: still 'open' on its own column

    assert slot_id not in _open_slot_ids(headers)


def test_demo_reset_leaves_the_dedicated_demo_slot_pool_available():
    result = subprocess.run(
        ["make", "demo-reset"], cwd=REPO, capture_output=True, text=True, env={**os.environ},
    )
    if result.returncode != 0:
        pytest.skip(f"could not run make demo-reset here: {result.stderr[:200]}")

    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM slots "
            "WHERE id BETWEEN %s AND %s AND status = 'open' AND start_at > now()",
            (_DEMO_SLOT_LO, _DEMO_SLOT_HI),
        )
        available = cur.fetchone()[0]

    assert available == (_DEMO_SLOT_HI - _DEMO_SLOT_LO + 1), (
        f"expected the full dedicated demo-booking pool open and in the future after reset, got {available}"
    )
