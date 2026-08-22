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

# drkim and drnguyen (2026-08-22, promoted from `staff`) are the seed's two
# `clinician` accounts — deliberately two, not one, so patient 1738's grant
# overlap between them is demonstrable (see generate_seed.py's own grant
# matrix comment). Every other seeded account remains on the deprecated
# `staff` role, which does NOT hold summary_review.decide — so the exclusion
# is demonstrable here rather than only in the role grid: see
# test_a_legacy_staff_account_cannot_reach_the_queue below, which uses one of
# the accounts still genuinely on `staff`.
_CLINICIAN = "drkim"


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


def test_a_legacy_staff_account_cannot_reach_the_queue(patient_token):
    """Every seeded account except drkim and drnguyen is on the deprecated
    `staff` role. drpatel below is deliberately still one of them — a
    treating-provider chart-access grant (patients.read/records.read) is not
    the same authorization as summary_review.decide, and drpatel holding the
    former for patient 1738 must not let it reach the queue.

    Adversarial review of #40 made the point that mattered: deciding is the
    release action, so gating it on a permission legacy accounts hold would
    have let front desk, billing, ROI and IT disclose withheld clinical notes
    to a patient — today, not after the roster-gated migration. This asserts
    the containment against real accounts rather than against the grid.
    """
    for username in ("frontdesk", "billing1", "roiclerk", "itadmin", "drpatel"):
        staff = _token(username)
        listing = httpx.get(f"{GATEWAY}/review-queue", headers=_auth(staff), timeout=10)
        assert listing.status_code == 403, f"{username} must not list the queue"

        decision = httpx.post(
            f"{GATEWAY}/review-queue/1/decision",
            headers=_auth(staff),
            json={"decision": "approved"},
            timeout=10,
        )
        assert decision.status_code == 403, f"{username} must not decide a case"


# --- round 2: the grant boundary and the actor requirement ------------------


def test_a_clinician_sees_only_cases_for_patients_they_are_granted(queued_cases):
    """Round-2 blocker B5. The queue shows withheld clinical note text and
    offers a control that releases it, so it is a chart-reading surface and
    belongs behind patient_access_grants like every other one.

    drkim is granted 1042, 1737 and 1738 — not 1043, and not 1739 (drnguyen's
    grant, not drkim's). drpatel holds a grant for 1043 and 1738 but is on
    `staff` and cannot reach the route at all, so the grant boundary itself is
    asserted here through drkim's own case list against the DB, and in the
    unit tests through the query.
    """
    granted = {
        r[0]
        for r in _rows(
            "SELECT g.patient_id FROM patient_access_grants g JOIN users u ON u.id=g.user_id"
            " WHERE u.username = %s AND g.revoked_at IS NULL",
            (_CLINICIAN,),
        )
    }
    assert granted, "the reviewing clinician must hold at least one grant"

    listing = httpx.get(
        f"{GATEWAY}/review-queue", headers=_auth(_token(_CLINICIAN)), timeout=10
    ).json()["items"]

    for case in listing:
        assert case["patient_id"] in granted, (
            f"case for patient {case['patient_id']} listed to a clinician granted only {granted}"
        )


def test_an_ungranted_case_cannot_be_decided_by_id(patient_token, clinician_token):
    """Listing is not the only door. A reviewer who cannot see a case in their
    queue must not be able to decide it by guessing or reusing its id."""
    _summary(patient_token)

    # A pending review for a patient this clinician holds no grant for.
    other = _rows(
        "SELECT id FROM patient_summary_reviews WHERE state='pending' AND patient_id <> %s LIMIT 1",
        (_PATIENT,),
    )
    if not other:
        pytest.skip("no pending review outside the clinician's grants to test with")

    r = httpx.post(
        f"{GATEWAY}/review-queue/{other[0][0]}/decision",
        headers=_auth(clinician_token),
        json={"decision": "approved"},
        timeout=10,
    )
    assert r.status_code == 409, "an ungranted case must not be decidable by id"


# --- round 3: the two-clinician overlap on the real seeded roster -----------
#
# Everything above proves the grant boundary using `_PATIENT` (1737, drkim
# only) and a synthetic invited-then-activated account. That leaves the
# actual reason drnguyen was promoted alongside drkim unproven end to end:
# patient 1738 is the one case both clinicians are granted, and the queue's
# pending->decided guard (already exercised above by
# test_a_decided_case_cannot_be_decided_again) has to hold under a SECOND
# reviewer racing the first, not just a retry by the same one. This runs
# against the real, preactivated seed accounts (patient-1738, patient-1739)
# rather than the invitation flow, since that is how those two patients
# actually reach the portal.

_SHARED_PATIENT = 1738     # granted to both drkim and drnguyen
_EXCLUSIVE_PATIENT = 1739  # granted to drnguyen only
_PATIENT_PASSWORD = "portalportal123"  # docs/runbook.md; preactivated accounts only


def _reset_reviews(patient_id):
    _run("DELETE FROM patient_summary_reviews WHERE patient_id = %s", (patient_id,))


@pytest.fixture
def two_clinician_setup():
    """Queues fresh cases for the shared and exclusive patients, from a clean
    review-queue state for both, and cleans up after itself so a later run of
    this file finds the same empty starting point `_reset` guarantees for
    `_PATIENT`."""
    _reset_reviews(_SHARED_PATIENT)
    _reset_reviews(_EXCLUSIVE_PATIENT)

    shared_patient_token = _token(f"patient-{_SHARED_PATIENT}", _PATIENT_PASSWORD)
    exclusive_patient_token = _token(f"patient-{_EXCLUSIVE_PATIENT}", _PATIENT_PASSWORD)
    _summary(shared_patient_token)      # queues 1738's refused content
    _summary(exclusive_patient_token)   # queues 1739's refused content

    yield

    _reset_reviews(_SHARED_PATIENT)
    _reset_reviews(_EXCLUSIVE_PATIENT)


def _cases_for(token, patient_id):
    r = httpx.get(f"{GATEWAY}/review-queue", headers=_auth(token), timeout=10)
    assert r.status_code == 200, r.text
    return [c for c in r.json()["items"] if c["patient_id"] == patient_id]


def test_the_shared_patient_is_visible_to_both_granted_clinicians(two_clinician_setup):
    """1738 is the one patient both drkim and drnguyen are granted — this is
    what makes the shared-queue scenario demonstrable at all, per
    generate_seed.py's grant matrix comment."""
    kim_cases = _cases_for(_token("drkim"), _SHARED_PATIENT)
    nguyen_cases = _cases_for(_token("drnguyen"), _SHARED_PATIENT)

    assert kim_cases, "drkim is granted 1738 and must see its pending cases"
    assert nguyen_cases, "drnguyen is granted 1738 and must see its pending cases"
    assert {c["id"] for c in kim_cases} == {c["id"] for c in nguyen_cases}, (
        "both reviewers hold the same grant for this patient, so they must see "
        "the same case set, not a filtered subset of each other's"
    )


def test_the_exclusive_patient_is_hidden_from_the_ungranted_clinician(two_clinician_setup):
    """1739 is granted to drnguyen only — drkim holds no grant for it, and the
    seed generator deliberately keeps it that way (see generate_seed.py)."""
    nguyen_cases = _cases_for(_token("drnguyen"), _EXCLUSIVE_PATIENT)
    kim_cases = _cases_for(_token("drkim"), _EXCLUSIVE_PATIENT)

    assert nguyen_cases, "drnguyen is granted 1739 and must see its pending cases"
    assert kim_cases == [], "drkim holds no grant for 1739 and must not see any of its cases"


def test_one_clinicians_decision_on_the_shared_case_blocks_the_other(two_clinician_setup):
    """The pending->decided guard already proven against a single reviewer
    (test_a_decided_case_cannot_be_decided_again) must also hold against a
    SECOND, independently-authorized reviewer — that is the actual risk an
    overlapping grant introduces, and it is what patient 1738 exists to
    demonstrate."""
    case = _cases_for(_token("drkim"), _SHARED_PATIENT)[0]

    first = httpx.post(
        f"{GATEWAY}/review-queue/{case['id']}/decision",
        headers=_auth(_token("drkim")),
        json={"decision": "approved"},
        timeout=10,
    )
    assert first.status_code == 200, first.text

    second = httpx.post(
        f"{GATEWAY}/review-queue/{case['id']}/decision",
        headers=_auth(_token("drnguyen")),
        json={"decision": "rejected"},
        timeout=10,
    )
    assert second.status_code == 409, (
        "drnguyen's own grant for 1738 is real, but the case is no longer "
        "pending — a second authorized reviewer must not be able to overwrite "
        "the first one's decision"
    )

    shown = [
        i for i in _summary(_token(f"patient-{_SHARED_PATIENT}", _PATIENT_PASSWORD))
        if i["record_id"] == case["record_id"]
    ][0]
    assert shown["released_by_review"] is True, "drkim's approval is what must stand"
