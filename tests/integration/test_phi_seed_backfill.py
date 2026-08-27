"""
Integration test — requires the full stack up (`make up`) on localhost.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).

w8-planner-2 P2 review round 1 fix (B1): `db/seed/seed.sql` is loaded
verbatim by Postgres's own docker-entrypoint-initdb.d on a fresh volume —
plain SQL, no way to reach libs/phi_crypto — so a fresh `make up` used to
leave every seeded patient's ssn/dob/notes plaintext with a NULL
ssn_digits, which silently broke blind-index-based duplicate detection for
the canonical Maria Gonzalez cluster (1042/1330/1588, adr/0004) — the
system's own worked example for this feature. `make up`/`make seed` now
run db/migrations/scripts/encrypt_existing_phi.py automatically
(Makefile's phi-backfill target) right after the seed load; this test
proves the cluster still clusters end to end through the real gateway
route, against whatever stack `make up` actually produced — not just that
the backfill script ran without raising.
"""
import os

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")


def _token_for(username: str) -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": "portal123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def test_seeded_maria_gonzalez_cluster_still_matches_after_backfill():
    # frontdesk (db/seed/generate_seed.py) holds an active grant for all
    # three rows in the cluster, so this exercises both halves of the
    # feature this PR touches: the blind-index match lookup itself
    # (reconciliation.py::find_ssn_match_ids) AND that records-service can
    # decrypt each matched candidate's dob back out for display.
    headers = {"Authorization": f"Bearer {_token_for('frontdesk')}"}

    r = httpx.get(f"{GATEWAY}/patients/1042/reconciliation", headers=headers, timeout=10)

    assert r.status_code == 200
    body = r.json()
    assert body["escalation"] is True

    matched_ids = {rec["patient_id"] for rec in body["source_records"]}
    assert matched_ids == {1042, 1330, 1588}

    # A blind index collision on garbage (e.g. every seeded row landing on
    # the same NULL/empty value) would produce this same shape by accident
    # — assert the decrypted dob actually came back too, not just that
    # rows were found.
    by_id = {rec["patient_id"]: rec for rec in body["source_records"]}
    assert by_id[1042]["dob"] == "1971-03-02"
    assert by_id[1330]["dob"] == "1971-03-02"
    assert by_id[1588]["dob"] == "1971-02-03"


def test_a_seeded_patient_with_no_cluster_shows_no_escalation():
    # Negative control: not every seeded patient should show a match —
    # otherwise the positive test above could pass on a blind index that
    # collides for every row into one bucket, "matching" everyone with
    # everyone.
    headers = {"Authorization": f"Bearer {_token_for('frontdesk')}"}

    r = httpx.get(f"{GATEWAY}/patients/1737/reconciliation", headers=headers, timeout=10)

    assert r.status_code == 200
    body = r.json()
    assert body["escalation"] is False
    assert {rec["patient_id"] for rec in body["source_records"]} == {1737}
