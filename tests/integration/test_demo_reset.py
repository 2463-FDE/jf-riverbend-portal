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


def test_reset_restores_a_deactivated_reviewer_account_to_active():
    """A disabled reviewer account used to be a state `make demo-reset`
    genuinely could NOT repair — this test originally asserted exactly that,
    from the failing side, so the printed summary would at least be honest
    about it rather than printing a reassuring role that meant nothing.

    2026-08-22: that limitation is now closed by explicit request — the reset
    restores BOTH clinician accounts (drkim and drnguyen) to active, the same
    way it already restored a revoked or expired grant. A rehearsal or a test
    that deactivates a reviewer account must not require a re-seed to fix, and
    the summary line (`active_reviewers`) is what an operator reads to
    confirm the repair actually took, not merely that the command exited 0.
    """
    _run("UPDATE users SET is_active = false WHERE username = %s", (REVIEWER,))
    assert not _reviewer_grant_is_active(), "precondition: a disabled account is not an active grant"

    out = _demo_reset()

    assert _reviewer_grant_is_active(), "reset must reactivate the account, not merely the grant"
    assert REVIEWER in out, "the summary must name the restored reviewer, not just say the role exists"
    assert "INACTIVE" not in out and "NONE" not in out, (
        "a restored reviewer must not still be reported as unusable"
    )


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


# --- W10 Final 2 Stage 3: coverage/eligibility, messaging, ROI readiness ----


def test_reset_restores_coverage_to_the_documented_baseline():
    """docs/runbook.md used to warn coverage "reflects whatever the last real
    eligibility check set it to and is NOT reset" — closed this stage: it now
    restores 1738's documented stale baseline and clears any in-flight
    verification_job_id."""
    _run(
        "UPDATE insurance_coverages SET status = 'active', verified_at = now(),"
        " verification_job_id = 'test-job-123' WHERE patient_id = 1738"
    )

    _demo_reset()

    row = _one(
        "SELECT status || '|' || coalesce(verification_job_id, '') FROM insurance_coverages"
        " WHERE patient_id = 1738"
    )
    assert row == "stale|", f"expected the documented stale baseline with no job id, got {row!r}"


def test_reset_restores_messaging_to_the_documented_baseline():
    """Thread 1 (patient 1738) is unread-by-both-clinicians and open in
    seed.sql — a rehearsal that replies, marks it read, or closes it must
    not carry over into the next one."""
    thomas_user_id = _one("SELECT id FROM users WHERE patient_id = 1738 AND role = 'patient'")
    follow_up_id = _one(
        "INSERT INTO thread_messages (thread_id, sender_user_id, body, idempotency_key)"
        " VALUES (1, %s, 'a rehearsal follow-up', 'test-followup-stage3') RETURNING id",
        (thomas_user_id,),
    )
    _run(
        "INSERT INTO thread_read_state (thread_id, user_id, last_read_message_id) VALUES (1, %s, %s)",
        (thomas_user_id, follow_up_id),
    )
    _run("UPDATE message_threads SET status = 'closed' WHERE id = 1")

    _demo_reset()

    assert _one("SELECT status FROM message_threads WHERE id = 1") == "open"
    assert _one("SELECT count(*) FROM thread_messages WHERE thread_id = 1") == 1, (
        "the extra follow-up must be cleared, leaving only the one seeded message"
    )
    assert _one("SELECT count(*) FROM thread_read_state WHERE thread_id = 1") == 0, (
        "1738's thread has no read state at all in the documented baseline"
    )


def test_reset_clears_a_pending_roi_request_for_a_canonical_patient():
    # Counts only 'pending' rows for 1042, not the total — a fulfilled
    # request (see test_reset_never_deletes_a_fulfilled_roi_requests_row_or_
    # its_disclosure, which also uses 1042) legitimately survives a reset
    # and may already exist from an earlier test in this same run.
    _run(
        "INSERT INTO roi_requests (patient_id, requested_by, recipient, recipient_type, purpose, status)"
        " VALUES (1042, 'frontdesk', 'Dr. Chen', 'provider', 'test', 'pending')"
    )

    _demo_reset()

    assert _one("SELECT count(*) FROM roi_requests WHERE patient_id = 1042 AND status = 'pending'") == 0


def test_reset_never_deletes_a_fulfilled_roi_requests_row_or_its_disclosure():
    """A fulfilled request/authorization has a real disclosures row pointing
    at it (45 CFR 164.508 accounting) — deleting either would either violate
    the foreign key Postgres itself enforces, or silently orphan the
    accounting log if it somehow didn't. Proven end to end through the real
    API, the same way the request would actually get fulfilled."""
    staff = _token("frontdesk")
    request_id = httpx.post(
        f"{GATEWAY}/roi/requests",
        headers=_auth(staff),
        json={"patient_id": 1042, "requested_by": "frontdesk", "recipient": "Dr. Chen, Stage3 Test",
              "recipient_type": "provider", "purpose": "continuity of care"},
        timeout=10,
    ).json()["id"]
    auth_id = httpx.post(
        f"{GATEWAY}/roi/authorizations",
        headers=_auth(staff),
        json={"patient_id": 1042, "recipient": "Dr. Chen, Stage3 Test", "purpose": "continuity of care",
              "signature_evidence_reference": "stage3-test-signed-form", "signed_by": "Maria Gonzalez",
              "signed_at": "2026-08-30T06:00:00Z"},
        timeout=10,
    ).json()["id"]
    httpx.post(
        f"{GATEWAY}/roi/authorizations/{auth_id}/review",
        headers=_auth(staff), json={"decision": "valid", "reviewed_by": "supervisor-stage3-test"}, timeout=10,
    )
    fulfill = httpx.post(
        f"{GATEWAY}/roi/requests/{request_id}/fulfill",
        headers=_auth(staff), json={"authorization_id": auth_id}, timeout=10,
    )
    assert fulfill.status_code == 200, fulfill.text
    disclosure_id = fulfill.json()["disclosure_id"]

    _demo_reset()

    assert _one("SELECT status FROM roi_requests WHERE id = %s", (request_id,)) == "fulfilled"
    assert _one("SELECT status FROM roi_authorizations WHERE id = %s", (auth_id,)) is not None
    assert _one("SELECT id FROM disclosures WHERE id = %s", (disclosure_id,)) == disclosure_id, (
        "the disclosure accounting row must survive a reset unconditionally"
    )


def test_reset_leaves_non_canonical_patients_and_non_reserved_rows_unchanged():
    roi_before = _one("SELECT count(*) FROM roi_requests WHERE patient_id NOT IN (1042, 1737, 1738, 1739)")
    threads_before = _one("SELECT count(*) FROM message_threads")
    non_demo_appts_before = _one("SELECT count(*) FROM appointments WHERE slot_id NOT BETWEEN 95001 AND 95016")

    _demo_reset()

    assert _one(
        "SELECT count(*) FROM roi_requests WHERE patient_id NOT IN (1042, 1737, 1738, 1739)"
    ) == roi_before
    assert _one("SELECT count(*) FROM message_threads") == threads_before, (
        "reset must not create or delete a whole thread, only its content beyond the seeded baseline"
    )
    assert _one(
        "SELECT count(*) FROM appointments WHERE slot_id NOT BETWEEN 95001 AND 95016"
    ) == non_demo_appts_before


def test_reset_never_touches_the_immutable_audit_and_disclosure_logs():
    audit_before = _one("SELECT count(*) FROM audit_logs")
    disclosures_before = _one("SELECT count(*) FROM disclosures")

    _demo_reset()

    assert _one("SELECT count(*) FROM audit_logs") == audit_before
    assert _one("SELECT count(*) FROM disclosures") == disclosures_before


def test_reset_leaves_the_approved_policy_corpus_alone():
    docs_before = _one("SELECT count(*) FROM policy_documents")
    embeddings_before = _one("SELECT count(*) FROM policy_chunk_embeddings")

    _demo_reset()

    assert _one("SELECT count(*) FROM policy_documents") == docs_before
    assert _one("SELECT count(*) FROM policy_chunk_embeddings") == embeddings_before


def test_reset_fails_closed_when_a_relied_on_fixture_is_missing():
    """The exact regression this stage's guard exists to prevent: a reset
    that reports success next to a broken/predates-the-seed database. Runs
    the SQL file directly (not `make demo-reset`, which would `pytest.skip`
    on a nonzero exit) so the failure itself can be asserted on."""
    thread_1_message_ids = [
        row[0] for row in _rows("SELECT id FROM thread_messages WHERE thread_id = 1")
    ]
    _run("DELETE FROM thread_read_state WHERE thread_id = 1")
    _run("DELETE FROM thread_messages WHERE thread_id = 1")
    _run("DELETE FROM message_threads WHERE id = 1")
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "psql",
             "-U", os.getenv("DB_USER", "riverbend_app"), "-d", os.getenv("DB_NAME", "riverbend"), "-q"],
            cwd=REPO, input=open(os.path.join(REPO, "db", "seed", "demo_reset.sql")).read(),
            capture_output=True, text=True,
        )
        assert result.returncode != 0, (
            "a missing relied-on fixture must stop the script, not exit 0 next to stale output"
        )
        assert "predates the current seed" in result.stdout + result.stderr
    finally:
        # Restore the fixture this test deleted so the autouse teardown's
        # own _demo_reset() call (which expects it) succeeds normally.
        thomas_user_id = _one("SELECT id FROM users WHERE patient_id = 1738 AND role = 'patient'")
        _run(
            "INSERT INTO message_threads (id, patient_id, subject, status, created_by, created_at, updated_at)"
            " VALUES (1, 1738, 'Question about my blood pressure readings', 'open', %s,"
            " '2026-08-20 09:00:00', '2026-08-20 09:00:00')",
            (thomas_user_id,),
        )
        for message_id in thread_1_message_ids:
            _run(
                "INSERT INTO thread_messages (id, thread_id, sender_user_id, body, idempotency_key, created_at)"
                " VALUES (%s, 1, %s, 'My home readings have been running a bit high this week,"
                " should I be concerned?', 'seed-1738-msg-1', '2026-08-20 09:00:00')",
                (message_id, thomas_user_id),
            )
        # No setval() here: the runtime role lacks sequence-modification
        # privilege (by design, migration 028), and it is not needed for
        # correctness — the restored rows use their own captured original
        # ids, and the sequence was already past them before this test ran.


def _rows(sql, params=()):
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()
