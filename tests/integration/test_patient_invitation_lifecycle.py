"""Integration tests — require the full stack up (`make up`) on localhost.

Invitation lifecycle at the database boundary (S1, migration 017).

These live here rather than in tests/test_patient_invitations.py because the
defect they cover is not expressible in the pure functions: it is a property
of the `patient_invitations_one_live_per_patient` partial index, and only a
real Postgres enforces it.

The defect (found in adversarial review of PR #34/#35): a partial-index
predicate must be IMMUTABLE, so it cannot call now(). The index therefore
cannot exclude *expired* rows, only activated and revoked ones — and a lapsed,
unactivated invitation kept its slot forever. The front desk got a 409 telling
them to revoke an invitation that had already expired, with no revoke route to
call. A patient who missed the 14-day window could never be issued another
code.

The fix closes expiry out in the application (gateway's
_revoke_lapsed_invitations) so the database sees a state its index CAN express.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).
"""
import os
import uuid

import pytest

httpx = pytest.importorskip("httpx")
psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")
DB_DSN = os.getenv(
    "DATABASE_URL", "postgresql://riverbend_app:riverbend_app_pw@localhost:5432/riverbend"
)

# A seeded demo patient (db/seed/generate_seed.py) — a real row, so these tests
# do not depend on fixture load order. No PHI is invented here.
_PATIENT_ID = 1042


def _token(username: str = "frontdesk", password: str = "portal123") -> str:
    r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _clear_invitations(patient_id: int) -> None:
    """Start from a known state. Deletes rather than revokes so the test is not
    reading through its own leftovers from a previous run."""
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM patient_invitations WHERE patient_id = %s", (patient_id,))
        conn.commit()


def _expire_open_invitation(patient_id: int) -> int:
    """Age the patient's open invitation past its window.

    Rewriting expires_at is how a 14-day wait is simulated without one. It
    changes only the timestamp — the row stays unactivated and unrevoked,
    which is exactly the state that used to be unrecoverable.
    """
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE patient_invitations
               SET expires_at = now() - interval '2 days',
                   issued_at  = now() - interval '16 days'
             WHERE patient_id = %s AND activated_at IS NULL AND revoked_at IS NULL
            """,
            (patient_id,),
        )
        count = cur.rowcount
        conn.commit()
    return count


def _invitation_rows(patient_id: int):
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, activated_at IS NULL, revoked_at IS NULL, expires_at > now()
              FROM patient_invitations WHERE patient_id = %s ORDER BY id
            """,
            (patient_id,),
        )
        return cur.fetchall()


@pytest.fixture
def clean_patient():
    _clear_invitations(_PATIENT_ID)
    yield _PATIENT_ID
    _clear_invitations(_PATIENT_ID)


def test_a_lapsed_invitation_does_not_block_reissue(clean_patient):
    """The defect itself: a patient who missed the window can be re-invited.

    Before the fix this second issue returned 409 permanently — the lapsed row
    held the one-open-invitation slot with no way to release it.
    """
    token = _token()

    first = httpx.post(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    )
    assert first.status_code == 201, first.text
    first_code = first.json()["code"]

    assert _expire_open_invitation(clean_patient) == 1, "the invitation should have been aged"

    second = httpx.post(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    )
    assert second.status_code == 201, (
        "reissue after expiry must succeed — a 409 here is the lockout returning: " + second.text
    )
    assert second.json()["code"] != first_code, "reissue must mint a fresh code"

    # The lapsed row is closed out, not deleted: it stays as the record that an
    # invitation was issued and never used.
    rows = _invitation_rows(clean_patient)
    assert len(rows) == 2, rows
    lapsed, live = rows
    assert lapsed[2] is False, "the lapsed invitation should now be revoked"
    assert live[2] is True and live[3] is True, "the replacement should be open and unexpired"


def test_the_lapsed_code_stops_working_once_it_is_replaced(clean_patient):
    """Reissuing must not leave the old code redeemable.

    This is the risk in fixing the lockout by hand-waving expiry: if the lapsed
    row were simply ignored rather than revoked, two codes would open one chart.
    """
    token = _token()
    first_code = httpx.post(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    ).json()["code"]
    _expire_open_invitation(clean_patient)
    httpx.post(f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10)

    redeemed = httpx.post(
        f"{GATEWAY}/patient/activate",
        json={"code": first_code, "password": "a-long-enough-passphrase"},
        timeout=10,
    )
    assert redeemed.status_code >= 400, "the superseded code must not activate an account"


def test_an_unexpired_invitation_still_blocks_a_second_one(clean_patient):
    """The constraint the fix must NOT weaken.

    Two live codes for one chart is two ways in, and revoking one would not
    close the other. Only *expired* rows are cleared on issue.
    """
    token = _token()
    first = httpx.post(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    )
    assert first.status_code == 201

    second = httpx.post(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    )
    assert second.status_code == 409, (
        "a genuinely live invitation must still refuse a second: " + second.text
    )
    assert "code" not in second.json(), "a refused issue must not leak a code"
    # Machine-readable, not English text (2026-08-22) — distinct from the
    # ACTIVE_PORTAL_ACCOUNT conflict, which the frontend must not offer a
    # revoke control for.
    assert second.json()["detail"]["reason"] == "LIVE_INVITATION"


def test_staff_can_revoke_an_outstanding_invitation_and_then_reissue(clean_patient):
    """The route the 409 tells staff to use — it did not exist before this fix.

    Covers the ordinary desk mistake too: a code read aloud to the wrong person
    has to be closable before its fourteen days run out.
    """
    token = _token()
    doomed = httpx.post(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    ).json()["code"]

    revoked = httpx.delete(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] == 1

    dead = httpx.post(
        f"{GATEWAY}/patient/activate",
        json={"code": doomed, "password": "a-long-enough-passphrase"},
        timeout=10,
    )
    assert dead.status_code >= 400, "a revoked code must not activate an account"

    again = httpx.post(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    )
    assert again.status_code == 201, "revoking must free the slot for a new code: " + again.text


def test_revoking_is_idempotent_and_reports_nothing_outstanding(clean_patient):
    token = _token()
    first = httpx.delete(
        f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10
    )
    assert first.status_code == 200
    assert first.json()["revoked"] == 0, "nothing was outstanding to revoke"


def test_revoking_requires_the_permission_to_issue(clean_patient):
    """Same gate as issuing. A role that cannot invite cannot cancel either."""
    token = _token()
    httpx.post(f"{GATEWAY}/patients/{clean_patient}/invitation", headers=_auth(token), timeout=10)

    anonymous = httpx.delete(f"{GATEWAY}/patients/{clean_patient}/invitation", timeout=10)
    assert anonymous.status_code in (401, 403), anonymous.text

    still_there = _invitation_rows(clean_patient)
    assert still_there and still_there[0][2] is True, "an unauthorized call must not revoke"
