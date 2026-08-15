"""Integration tests — require the full stack up (`make up`) on localhost.

The clinician gate, end to end (S3).

The client rejected a review queue that is "a table plus a screen": the
approve/reject decision has to control what the patient can actually see. The
assertions here are written against that requirement — the central one is not
that a decision is recorded, but that the patient's own results change as a
result of it.

Run with:  pytest -m integration
Skipped by default in CI (`pytest -m "not integration"`).
"""
import os

import pytest

httpx = pytest.importorskip("httpx")
psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")
DB_DSN = os.getenv(
    "DATABASE_URL", "postgresql://riverbend_app:riverbend_app_pw@localhost:5432/riverbend"
)

# 1737's chart carries visit notes the renderer cannot quote, which is what
# makes it the patient that exercises the refusal path (see
# tests/integration/test_patient_summary_flow.py for the same choice).
_PATIENT = 1737
_PASSWORD = "portal-patient-passphrase"

# drpatel is a seeded clinician. NOTE: every seeded account is still on the
# deprecated `staff` role, which retains records.write — so this test proves
# the route admits a clinician, NOT that it excludes other staff. The role
# grid's exclusion is asserted separately and cheaply in
# tests/test_review_queue_gate.py, because it cannot be demonstrated here until
# the account migration runs.
_CLINICIAN = "drpatel"


def _run(sql, params=()):
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()


def _rows(sql, params=()):
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _token(username, password="portal123"):
    r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _reset():
    _run("DELETE FROM patient_summary_reviews WHERE patient_id = %s", (_PATIENT,))
    _run(
        "DELETE FROM patient_access_grants WHERE user_id IN"
        " (SELECT id FROM users WHERE patient_id = %s)",
        (_PATIENT,),
    )
    _run("DELETE FROM patient_invitations WHERE patient_id = %s", (_PATIENT,))
    _run("DELETE FROM users WHERE patient_id = %s", (_PATIENT,))


@pytest.fixture
def patient_token():
    _reset()
    staff = _token("frontdesk")
    issued = httpx.post(
        f"{GATEWAY}/patients/{_PATIENT}/invitation", headers=_auth(staff), timeout=10
    )
    code = issued.json()["code"]
    httpx.post(f"{GATEWAY}/patient/activate", json={"code": code, "password": _PASSWORD}, timeout=10)
    yield _token(f"patient-{_PATIENT}", _PASSWORD)
    _reset()


@pytest.fixture
def clinician_token():
    return _token(_CLINICIAN)


def _summary(token):
    r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(token), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _cases(token):
    r = httpx.get(f"{GATEWAY}/review-queue", headers=_auth(token), timeout=10)
    assert r.status_code == 200, r.text
    return [c for c in r.json()["items"] if c["patient_id"] == _PATIENT]


@pytest.fixture
def queued_cases(patient_token, clinician_token):
    """Cases waiting on a clinician for this patient.

    The patient's own read is what queues them — that is the design, not a
    quirk: the queue holds exactly what a patient was refused. So the read has
    to happen before there is anything for a clinician to look at, and every
    test needing a case goes through here rather than reaching for the queue
    first and finding it empty.
    """
    _summary(patient_token)
    cases = _cases(clinician_token)
    assert cases, "the patient's read should have queued refused content"
    return cases


def test_content_the_patient_is_refused_reaches_the_queue(patient_token, clinician_token):
    """The queue contains exactly what patients could not see. That equality is
    what makes it real rather than a parallel worklist."""
    refused = [i for i in _summary(patient_token) if i["refusal_reason"]]
    assert refused, "fixture patient must have refused content for this to mean anything"

    queued = _rows(
        "SELECT record_id FROM patient_summary_reviews WHERE patient_id = %s", (_PATIENT,)
    )
    assert {r[0] for r in queued} == {i["record_id"] for i in refused}

    cases = _cases(clinician_token)
    assert cases and cases[0]["record_body"], (
        "the clinician must see the source text — approving content you have not read "
        "is the failure this screen exists to prevent"
    )


def test_reading_the_summary_repeatedly_does_not_grow_the_queue(patient_token):
    """Queueing happens on the patient's read path, so it has to be
    idempotent — otherwise a refresh becomes a denial-of-service on the
    clinician."""
    _summary(patient_token)   # the first read is what creates the rows
    before = _rows("SELECT count(*) FROM patient_summary_reviews WHERE patient_id=%s", (_PATIENT,))[0][0]
    assert before, "the first read should have queued something"

    _summary(patient_token)
    _summary(patient_token)

    after = _rows("SELECT count(*) FROM patient_summary_reviews WHERE patient_id=%s", (_PATIENT,))[0][0]
    assert after == before


def test_a_rejected_review_never_becomes_patient_visible(patient_token, clinician_token, queued_cases):
    """The client's acceptance criterion, asserted end to end."""
    case = queued_cases[0]

    decision = httpx.post(
        f"{GATEWAY}/review-queue/{case['id']}/decision",
        headers=_auth(clinician_token),
        json={"decision": "rejected", "note": "not appropriate to release"},
        timeout=10,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["patient_visible"] is False

    shown = [i for i in _summary(patient_token) if i["record_id"] == case["record_id"]][0]
    assert shown["quote"] is None
    assert shown["refusal_reason"]
    assert shown["released_by_review"] is False

    # And it stays rejected: re-queueing would let the next page view undo the
    # clinician's decision.
    assert _rows(
        "SELECT count(*) FROM patient_summary_reviews WHERE record_id=%s", (case["record_id"],)
    )[0][0] == 1


def test_an_approved_review_releases_the_records_own_words(patient_token, clinician_token, queued_cases):
    case = queued_cases[0]
    record_id = case["record_id"]

    before = [i for i in _summary(patient_token) if i["record_id"] == record_id][0]
    assert before["quote"] is None, "must start withheld, or this proves nothing"

    decision = httpx.post(
        f"{GATEWAY}/review-queue/{case['id']}/decision",
        headers=_auth(clinician_token),
        json={"decision": "approved"},
        timeout=10,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["patient_visible"] is True

    after = [i for i in _summary(patient_token) if i["record_id"] == record_id][0]
    stored = _rows("SELECT body FROM records WHERE id = %s", (record_id,))[0][0]

    assert after["quote"] == stored.strip(), "approval releases the report's own words, verbatim"
    assert after["refusal_reason"] is None
    assert after["released_by_review"] is True, "the patient is told a clinician released this"
    assert after["change"] is None, "releasing text is not licence to start computing on it"


def test_approval_releases_only_the_record_it_named(patient_token, clinician_token, queued_cases):
    """A blanket release is the failure a per-record gate exists to prevent."""
    cases = queued_cases
    if len(cases) < 2:
        pytest.skip("needs at least two queued cases")
    approved, untouched = cases[0], cases[1]

    httpx.post(
        f"{GATEWAY}/review-queue/{approved['id']}/decision",
        headers=_auth(clinician_token),
        json={"decision": "approved"},
        timeout=10,
    )

    items = {i["record_id"]: i for i in _summary(patient_token)}
    assert items[approved["record_id"]]["quote"] is not None
    assert items[untouched["record_id"]]["quote"] is None


def test_a_decided_case_cannot_be_decided_again(patient_token, clinician_token, queued_cases):
    """A stale screen or a double click must not overwrite a recorded
    decision."""
    case = queued_cases[0]
    first = httpx.post(
        f"{GATEWAY}/review-queue/{case['id']}/decision",
        headers=_auth(clinician_token),
        json={"decision": "rejected"},
        timeout=10,
    )
    assert first.status_code == 200

    second = httpx.post(
        f"{GATEWAY}/review-queue/{case['id']}/decision",
        headers=_auth(clinician_token),
        json={"decision": "approved"},
        timeout=10,
    )
    assert second.status_code == 409, "a decided case is no longer pending"


def test_a_patient_cannot_reach_the_review_queue(patient_token):
    """The queue is staff-facing. The patient role holds no staff permission."""
    listing = httpx.get(f"{GATEWAY}/review-queue", headers=_auth(patient_token), timeout=10)
    assert listing.status_code in (401, 403)

    decision = httpx.post(
        f"{GATEWAY}/review-queue/1/decision",
        headers=_auth(patient_token),
        json={"decision": "approved"},
        timeout=10,
    )
    assert decision.status_code in (401, 403), "a patient must not be able to release their own content"


def test_the_review_queue_refuses_an_unauthenticated_caller():
    assert httpx.get(f"{GATEWAY}/review-queue", timeout=10).status_code in (401, 403)
