"""Integration tests — require the full stack up (`make up`) on localhost.

Stage 4 (Week 5, RIV-175, migration 013): proves the double-booking race is
actually closed under real concurrency against the real Postgres instance,
not just in tests/test_scheduling_book.py's fake-connection unit coverage.
book.py used to have an artificial time.sleep(0.05) between its check and
its insert specifically to make this race easy to hit in a demo — that
sleep is gone now, so this fires genuinely concurrent requests instead.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).
"""
import concurrent.futures
import os
import random
import uuid

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")

# Seeded demo patients (db/seed/generate_seed.py) — real rows, not fixtures
# this test creates itself, so no dependency on load order.
_COMPETING_PATIENT_IDS = [1042, 1043, 1330, 1588, 1601, 1602, 1603, 1604]


def _token() -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": "frontdesk", "password": "portal123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _fresh_slot_id() -> int:
    # Deliberately NOT "the next open slot from GET /slots": slots.status is
    # pre-existing, documented-as-advisory-only debt (never flipped to
    # 'booked' by the booking flow — see services/scheduling-service/models.py),
    # so an already-confirmed slot keeps showing up as "open" indefinitely.
    # That's a separate, tangential gap from RIV-175's actual double-booking
    # race and out of scope here. appointments.slot_id has no FK to slots(id)
    # at all (db/schema.sql's own comment), so a synthetic id well outside the
    # seeded 88200-88320 range exercises the exact same UNIQUE index this test
    # is about, without depending on that unrelated gap or on run-to-run seed
    # state.
    return random.randint(900_000, 999_999)


def test_concurrent_bookings_for_the_same_slot_confirm_exactly_once():
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id = _fresh_slot_id()

    def _attempt(patient_id: int):
        return httpx.post(
            f"{GATEWAY}/appointments",
            json={
                "patient_id": patient_id,
                "slot_id": slot_id,
                "idempotency_key": str(uuid.uuid4()),  # a genuinely distinct booking attempt each
                "reason": "RIV-175 concurrency test",
            },
            headers=headers,
            timeout=10,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_COMPETING_PATIENT_IDS)) as pool:
        responses = list(pool.map(_attempt, _COMPETING_PATIENT_IDS))

    for r in responses:
        assert r.status_code == 201, r.text

    confirmed = [r for r in responses if r.json()["status"] == "confirmed"]
    slot_taken = [r for r in responses if r.json()["status"] == "slot_taken"]

    assert len(confirmed) == 1, f"expected exactly one confirmed booking for the same slot, got {len(confirmed)}"
    assert len(slot_taken) == len(_COMPETING_PATIENT_IDS) - 1


def test_retrying_the_same_idempotency_key_returns_the_same_appointment_not_a_second_one():
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id = _fresh_slot_id()
    key = str(uuid.uuid4())
    payload = {
        "patient_id": 1042,
        "slot_id": slot_id,
        "idempotency_key": key,
        "reason": "RIV-175 idempotency retry test",
    }

    first = httpx.post(f"{GATEWAY}/appointments", json=payload, headers=headers, timeout=10)
    second = httpx.post(f"{GATEWAY}/appointments", json=payload, headers=headers, timeout=10)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["status"] == "confirmed"
    assert second.json()["status"] == "confirmed"
    assert first.json()["appointment_id"] == second.json()["appointment_id"]


def test_concurrent_retries_of_the_same_idempotency_key_all_agree_on_one_appointment():
    # The other half of the SAVEPOINT design: not just a sequential retry
    # (above), but N simultaneous requests replaying the SAME key — exactly
    # one insert should happen; every response (winner and racers alike)
    # must report the SAME appointment_id.
    headers = {"Authorization": f"Bearer {_token()}"}
    slot_id = _fresh_slot_id()
    key = str(uuid.uuid4())
    payload = {
        "patient_id": 1043,
        "slot_id": slot_id,
        "idempotency_key": key,
        "reason": "RIV-175 concurrent idempotency retry test",
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
