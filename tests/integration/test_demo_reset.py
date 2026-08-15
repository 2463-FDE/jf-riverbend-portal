"""Integration tests — require the full stack up (`make up`) on localhost.

`make demo-reset` is the tool that gates every rehearsal, and it shipped a bug
that reported success while the demo was broken: the grant insert was guarded
on a row EXISTING, while the review queue is scoped on that row being ACTIVE
(`revoked_at IS NULL`, unexpired). A revoked grant therefore suppressed the
insert, left the reviewer unable to see anything, and the script still printed
a healthy-looking line.

That failure mode — a reset that says it worked next to an empty queue — is
worse than one that fails loudly, because the operator discovers it mid-demo
with nothing on screen connecting the two. So the reset gets a test of its own,
driven from the exact broken state rather than from a clean one.

Run with:  pytest -m integration
"""
import os
import subprocess

import pytest

httpx = pytest.importorskip("httpx")
psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")
DB_DSN = os.getenv(
    "DATABASE_URL", "postgresql://riverbend_app:riverbend_app_pw@localhost:5432/riverbend"
)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEMO_PATIENT = 1737
REVIEWER = "drkim"
PASSWORD = "portal-patient-passphrase"


def _run(sql, params=()):
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()


def _one(sql, params=()):
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def _demo_reset():
    """Invoke the real thing, the way an operator does."""
    result = subprocess.run(
        ["make", "demo-reset"], cwd=REPO, capture_output=True, text=True,
        env={**os.environ},
    )
    if result.returncode != 0:
        pytest.skip(f"could not run make demo-reset here: {result.stderr[:200]}")
    return result.stdout


def _reviewer_grant_is_active() -> bool:
    """The gate's own predicate — see patient_access_gate.active_patient_ids_query."""
    return bool(
        _one(
            """
            SELECT 1 FROM patient_access_grants g JOIN users u ON u.id = g.user_id
             WHERE u.username = %s AND u.is_active AND g.patient_id = %s
               AND g.revoked_at IS NULL
               AND (g.expires_at IS NULL OR g.expires_at > now())
            """,
            (REVIEWER, DEMO_PATIENT),
        )
    )


def _token(username, password="portal123"):
    r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _walk_to_a_populated_queue():
    """Activate the patient, read the summary so refusals queue, return cases."""
    staff = _token("frontdesk")
    code = httpx.post(
        f"{GATEWAY}/patients/{DEMO_PATIENT}/invitation", headers=_auth(staff), timeout=10
    ).json()["code"]
    httpx.post(f"{GATEWAY}/patient/activate", json={"code": code, "password": PASSWORD}, timeout=10)
    patient = _token(f"patient-{DEMO_PATIENT}", PASSWORD)
    httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient), timeout=10)

    reviewer = _token(REVIEWER)
    listing = httpx.get(f"{GATEWAY}/review-queue", headers=_auth(reviewer), timeout=10)
    assert listing.status_code == 200, listing.text
    return [c for c in listing.json()["items"] if c["patient_id"] == DEMO_PATIENT]


@pytest.fixture(autouse=True)
def leave_the_demo_ready():
    """Every test here mutates demo state; hand it back usable."""
    yield
    _demo_reset()


# --- the regression that prompted this file --------------------------------


def test_reset_restores_a_reviewer_grant_that_was_revoked():
    """The exact bug: guarded on existence, not on being active."""
    _run(
        "UPDATE patient_access_grants SET revoked_at = now()"
        " WHERE user_id = (SELECT id FROM users WHERE username = %s) AND patient_id = %s",
        (REVIEWER, DEMO_PATIENT),
    )
    assert not _reviewer_grant_is_active(), "precondition: the grant must start revoked"

    _demo_reset()

    assert _reviewer_grant_is_active(), "reset must restore an ACTIVE grant, not merely a row"
    assert _walk_to_a_populated_queue(), "and the reviewer must actually see cases"


def test_reset_restores_a_reviewer_grant_that_had_expired():
    """The other half of the gate's predicate. An expired grant is just as
    inert as a revoked one, and just as invisible to a row-existence check."""
    _run(
        "UPDATE patient_access_grants SET expires_at = now() - interval '1 day'"
        " WHERE user_id = (SELECT id FROM users WHERE username = %s) AND patient_id = %s",
        (REVIEWER, DEMO_PATIENT),
    )
    assert not _reviewer_grant_is_active()

    _demo_reset()

    assert _reviewer_grant_is_active()


def test_the_printed_verification_reports_a_state_the_reset_cannot_fix():
    """The second half of the original bug, tested from the failing side.

    The old line printed the account's ROLE, which is true whether or not that
    reviewer can see anything — so what an operator trusts was decoupled from
    what breaks. Asserting it says "active" on a healthy run proves little: the
    buggy version said something reassuring too.

    So this creates a state the reset genuinely cannot repair — a disabled
    reviewer account, which the gate's predicate rejects via u.is_active — and
    requires the summary to SAY so. A verification line that cannot report
    failure is not a verification line.
    """
    _run("UPDATE users SET is_active = false WHERE username = %s", (REVIEWER,))
    try:
        out = _demo_reset()
        assert "INACTIVE" in out, (
            "the summary must report an unusable reviewer, not a reassuring role"
        )
        assert not _reviewer_grant_is_active()
    finally:
        _run("UPDATE users SET is_active = true WHERE username = %s", (REVIEWER,))


# --- the everyday job ------------------------------------------------------


def test_reset_returns_a_consumed_demo_to_a_runnable_state():
    """The reason the command exists: decisions are durable, so a rehearsal
    consumes cases and the next run would otherwise start mid-story."""
    cases = _walk_to_a_populated_queue()
    assert len(cases) >= 2, "need cases to consume"

    reviewer = _token(REVIEWER)
    for case, decision in ((cases[0], "approved"), (cases[1], "rejected")):
        httpx.post(
            f"{GATEWAY}/review-queue/{case['id']}/decision",
            headers=_auth(reviewer),
            json={"decision": decision},
            timeout=10,
        )
    assert _one(
        "SELECT count(*) FROM patient_summary_reviews WHERE patient_id = %s AND state <> 'pending'",
        (DEMO_PATIENT,),
    ) == 2

    _demo_reset()

    assert _one(
        "SELECT count(*) FROM patient_summary_reviews WHERE patient_id = %s", (DEMO_PATIENT,)
    ) == 0, "decisions must be cleared"
    assert _one(
        "SELECT count(*) FROM users WHERE patient_id = %s", (DEMO_PATIENT,)
    ) == 0, "the portal account must be gone so the demo starts at 'issue a code'"

    assert len(_walk_to_a_populated_queue()) >= 2, "and the queue must repopulate"


def test_reset_is_safe_to_run_twice():
    """Operators will. It must not fail or half-apply on the second run."""
    _demo_reset()
    _demo_reset()

    assert _reviewer_grant_is_active()
    assert _one("SELECT count(*) FROM users WHERE patient_id = %s", (DEMO_PATIENT,)) == 0


def test_reset_leaves_the_chart_alone():
    """It clears portal state, not clinical data. Deleting records would take
    the A1c trend with it and quietly break the summary beat instead."""
    before = _one(
        "SELECT count(*) FROM records WHERE patient_id = %s", (DEMO_PATIENT,)
    )
    _demo_reset()
    assert _one("SELECT count(*) FROM records WHERE patient_id = %s", (DEMO_PATIENT,)) == before
    assert _one(
        "SELECT count(*) FROM records WHERE patient_id = %s AND title = 'A1c'", (DEMO_PATIENT,)
    ) == 2, "the demo A1c pair must survive a reset"
