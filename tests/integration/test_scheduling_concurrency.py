"""Integration tests — require the full stack up (`make up`) on localhost.

Stage 4 (Week 5, RIV-175, migration 013): proves the double-booking race is
actually closed under real concurrency against the real Postgres instance,
not just in tests/test_scheduling_book.py's fake-connection unit coverage.
book.py used to have an artificial time.sleep(0.05) between its check and
its insert specifically to make this race easy to hit in a demo — that
sleep is gone now, so this fires genuinely concurrent requests instead.

w9-fixes P0 4.4 (2026-08-23): this file used to write directly into the
shared demo database with no cleanup — every run left a real "RIV-175 ...
test" appointment behind on canonical patient 1042, occupying a real slot
forever (that string showing up in the ordinary Appointments UI was test
pollution, not seed data). It also competed for patients (1043, 1602-1604)
frontdesk holds no grant for, and used a synthetic out-of-range slot id
(random.randint(900_000, 999_999)) — both worked only because scheduling
enforced neither a per-patient grant nor a real, open, future slot. Both
gaps are closed now (see services/gateway/app.py's grant checks and
book.py's _lock_open_slot), so this file:
  * only competes with patient ids frontdesk actually holds an active grant
    for (services/gateway would otherwise deny every request with 403
    before scheduling-service ever sees it);
  * creates its own throwaway, real, future 'open' slot row per test
    instead of relying on a synthetic id or the shared demo pool; and
  * deletes every appointment and slot row it created, in a fixture
    finalizer that runs even if the test body raises.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).
"""
import concurrent.futures
import os
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

# frontdesk's actual active patient_access_grants (verified against the live
# demo db; also asserted by test_gateway_appointment_authorization.py's unit
# coverage of the grant check itself). NOT the full canonical-patient list —
# frontdesk deliberately lacks 1043 (see db/seed/generate_seed.py's own
# comment on that), among others, and a booking attempt for a patient
# frontdesk isn't granted now gets a 403 before scheduling-service is ever
# called, which would silently change what this test is actually proving.
_COMPETING_PATIENT_IDS = [1042, 1330, 1588, 1601, 1629, 1737, 1738, 1739]


def _token() -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": "frontdesk", "password": "portal123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _create_open_slot(days_ahead: int = 3) -> int:
    """A real, throwaway slot row this test owns end-to-end — never a shared
    seed/demo slot and never a synthetic nonexistent id. book.py's
    _lock_open_slot now requires the slot to actually exist, be 'open', and
    have a future start_at, so a synthetic out-of-range id (the previous
    approach) is rejected outright rather than exercising the race."""
    start = datetime.utcnow() + timedelta(days=days_ahead)
    end = start + timedelta(minutes=30)
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slots (provider_id, location, start_at, end_at, status) "
            "VALUES (1, 'Riverbend Main', %s, %s, 'open') RETURNING id",
            (start, end),
        )
        slot_id = cur.fetchone()[0]
        conn.commit()
        return slot_id


@pytest.fixture
def scheduling_cleanup():
    """Tracks every slot/appointment id a test creates and removes all of
    them afterward, even on failure — the exact isolation this file lacked
    before (w9-fixes P0 4.4)."""
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


def test_concurrent_bookings_for_the_same_slot_confirm_exactly_once(scheduling_cleanup):
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id = _create_open_slot()
    scheduling_cleanup["slot_ids"].append(slot_id)

    def _attempt(patient_id: int):
        return httpx.post(
            f"{GATEWAY}/appointments",
            json={
                "patient_id": patient_id,
                "slot_id": slot_id,
                "idempotency_key": str(uuid.uuid4()),  # a genuinely distinct booking attempt each
                "reason": "concurrency test",
            },
            headers=headers,
            timeout=10,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_COMPETING_PATIENT_IDS)) as pool:
        responses = list(pool.map(_attempt, _COMPETING_PATIENT_IDS))

    for r in responses:
        if r.status_code == 201:
            scheduling_cleanup["appointment_ids"].append(r.json()["appointment_id"])

    # Round-22 review (2026-08-06): a losing booking is now a real 409, not
    # a 201 with status="slot_taken" in the body — a losing booker must see
    # a failure, not something r.ok would treat as success.
    confirmed = [r for r in responses if r.status_code == 201]
    slot_taken = [r for r in responses if r.status_code == 409]

    assert len(confirmed) == 1, f"expected exactly one confirmed booking for the same slot, got {len(confirmed)}"
    assert confirmed[0].json()["status"] == "confirmed"
    assert len(slot_taken) == len(_COMPETING_PATIENT_IDS) - 1
    assert all(r.json()["detail"]["error"] == "slot_taken" for r in slot_taken)


def test_retrying_the_same_idempotency_key_returns_the_same_appointment_not_a_second_one(scheduling_cleanup):
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id = _create_open_slot()
    scheduling_cleanup["slot_ids"].append(slot_id)
    key = str(uuid.uuid4())
    payload = {
        "patient_id": 1042,
        "slot_id": slot_id,
        "idempotency_key": key,
        "reason": "idempotency retry test",
    }

    first = httpx.post(f"{GATEWAY}/appointments", json=payload, headers=headers, timeout=10)
    second = httpx.post(f"{GATEWAY}/appointments", json=payload, headers=headers, timeout=10)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["status"] == "confirmed"
    assert second.json()["status"] == "confirmed"
    assert first.json()["appointment_id"] == second.json()["appointment_id"]
    scheduling_cleanup["appointment_ids"].append(first.json()["appointment_id"])


def test_reusing_an_idempotency_key_for_a_different_slot_is_a_conflict_not_a_replay(scheduling_cleanup):
    # Round-22 review (2026-08-06): the review's explicit ask — a reused
    # key with a genuinely different request must never be silently
    # treated as "the same booking, here's your original confirmation."
    headers = {"Authorization": f"Bearer {_token()}"}
    key = str(uuid.uuid4())
    first_slot = _create_open_slot(days_ahead=3)
    second_slot = _create_open_slot(days_ahead=4)
    scheduling_cleanup["slot_ids"].extend([first_slot, second_slot])

    first = httpx.post(
        f"{GATEWAY}/appointments",
        json={"patient_id": 1601, "slot_id": first_slot, "idempotency_key": key, "reason": "original booking"},
        headers=headers,
        timeout=10,
    )
    assert first.status_code == 201
    scheduling_cleanup["appointment_ids"].append(first.json()["appointment_id"])

    second = httpx.post(
        f"{GATEWAY}/appointments",
        json={"patient_id": 1601, "slot_id": second_slot, "idempotency_key": key, "reason": "original booking"},
        headers=headers,
        timeout=10,
    )

    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "idempotency_key_conflict"
    assert second.json()["detail"]["existing_appointment_id"] == first.json()["appointment_id"]


def test_concurrent_retries_of_the_same_idempotency_key_all_agree_on_one_appointment(scheduling_cleanup):
    # The other half of the SAVEPOINT design: not just a sequential retry
    # (above), but N simultaneous requests replaying the SAME key — exactly
    # one insert should happen; every response (winner and racers alike)
    # must report the SAME appointment_id.
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id = _create_open_slot()
    scheduling_cleanup["slot_ids"].append(slot_id)
    key = str(uuid.uuid4())
    payload = {
        "patient_id": 1737,
        "slot_id": slot_id,
        "idempotency_key": key,
        "reason": "concurrent idempotency retry test",
    }

    def _attempt(_):
        return httpx.post(f"{GATEWAY}/appointments", json=payload, headers=headers, timeout=10)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(_attempt, range(6)))

    for r in responses:
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "confirmed"

    appointment_ids = {r.json()["appointment_id"] for r in responses}
    assert len(appointment_ids) == 1, f"expected every retry to agree on one appointment_id, got {appointment_ids}"
    scheduling_cleanup["appointment_ids"].append(appointment_ids.pop())
