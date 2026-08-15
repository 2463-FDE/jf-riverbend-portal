"""Integration tests — require the full stack up (`make up`) on localhost.

THE ACCEPTANCE PATH (client, for the 2026-08-28 freeze).

The other integration files each prove one component. This one walks the whole
purchased product in a single sequence, in the client's own terms, so the
acceptance conversation can be had against a test run rather than a demo
script:

    front desk issues invitation
      -> patient activates
      -> patient signs in
      -> own-record authorization
      -> summary generated
           |- direct fact  -> patient
           |- simple delta -> patient
           `- inference    -> review queue
      -> clinician decision
      -> patient result

The negative tests below are the client's list, not ours, and they are the
point of the exercise: a passing happy path proves nothing here. Each is named
after the criterion it satisfies so a reader can map them one to one.

Deliberately duplicates a few assertions made in the component files. That is
not redundancy to remove — those files prove a unit behaves; this one proves
the assembled product does, and the two can diverge.

Run with:  pytest -m integration
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

# 1737 exercises all three content outcomes on one chart: a panel, a repeated
# single value, and prose that must refuse. 1629 is the other patient, used
# only to prove they cannot be reached.
PATIENT_A = 1737
PATIENT_B = 1629
PASSWORD = "portal-patient-passphrase"

FRONT_DESK = "frontdesk"
CLINICIAN = "drkim"      # the seed's one account on the `clinician` role


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


def _reset(patient_id):
    _run("DELETE FROM patient_summary_reviews WHERE patient_id = %s", (patient_id,))
    _run(
        "DELETE FROM patient_access_grants WHERE user_id IN"
        " (SELECT id FROM users WHERE patient_id = %s)",
        (patient_id,),
    )
    _run("DELETE FROM patient_invitations WHERE patient_id = %s", (patient_id,))
    _run("DELETE FROM users WHERE patient_id = %s", (patient_id,))


@pytest.fixture
def clean():
    for p in (PATIENT_A, PATIENT_B):
        _reset(p)
    yield
    for p in (PATIENT_A, PATIENT_B):
        _reset(p)


def _activate(patient_id, staff_token):
    """Front desk issues; the patient redeems. Returns the patient's token."""
    issued = httpx.post(
        f"{GATEWAY}/patients/{patient_id}/invitation", headers=_auth(staff_token), timeout=10
    )
    assert issued.status_code == 201, issued.text
    code = issued.json()["code"]
    assert code, "the code is returned exactly once, here"

    activated = httpx.post(
        f"{GATEWAY}/patient/activate", json={"code": code, "password": PASSWORD}, timeout=10
    )
    assert activated.status_code == 200, activated.text
    return _token(f"patient-{patient_id}", PASSWORD)


def _summary(token):
    r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(token), timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# --------------------------------------------------------------------------
# The path itself
# --------------------------------------------------------------------------


def test_the_whole_purchased_product_end_to_end(clean):
    """One sequence, front desk to patient, with a clinician in the middle."""
    staff = _token(FRONT_DESK)
    clinician = _token(CLINICIAN)

    # 1-4. issue -> activate -> sign in -> read own record.
    patient = _activate(PATIENT_A, staff)
    items = _summary(patient)
    assert items, "an activated patient must reach their own results"

    by_shape = {}
    for i in items:
        by_shape.setdefault(i["shape"], []).append(i)

    # 5a. Direct fact reaches the patient with no clinician involved.
    singles = by_shape.get("single_value", [])
    assert singles, "the fixture chart must contain a quotable single value"
    for item in singles:
        assert item["quote"], "a directly-supported fact is shown as-is"
        assert item["released_by_review"] is False, "and needed nobody's permission"

    # 5b. Simple delta reaches the patient — arithmetic, not judgment.
    with_change = [i for i in items if i.get("change")]
    assert with_change, "a repeated single value must produce a trend"
    for item in with_change:
        assert item["shape"] == "single_value"
        assert item["change"]["direction"] in ("up", "down", "unchanged")

    # 5c. Inference goes to the queue instead of the patient.
    refused = by_shape.get("unquotable", [])
    assert refused, "the fixture chart must contain content that cannot be quoted"
    for item in refused:
        assert item["quote"] is None
        assert item["refusal_reason"]

    queued = _rows(
        "SELECT record_id FROM patient_summary_reviews WHERE patient_id=%s AND state='pending'",
        (PATIENT_A,),
    )
    assert {r[0] for r in queued} == {i["record_id"] for i in refused}, (
        "the queue must hold exactly what the patient was refused"
    )

    # 6. Clinician decision.
    cases = [
        c
        for c in httpx.get(f"{GATEWAY}/review-queue", headers=_auth(clinician), timeout=10).json()[
            "items"
        ]
        if c["patient_id"] == PATIENT_A
    ]
    assert len(cases) >= 2, "need one to release and one to withhold"
    release, withhold = cases[0], cases[1]

    approved = httpx.post(
        f"{GATEWAY}/review-queue/{release['id']}/decision",
        headers=_auth(clinician),
        json={"decision": "approved"},
        timeout=10,
    )
    assert approved.status_code == 200 and approved.json()["patient_visible"] is True

    rejected = httpx.post(
        f"{GATEWAY}/review-queue/{withhold['id']}/decision",
        headers=_auth(clinician),
        json={"decision": "rejected", "note": "discuss at next visit"},
        timeout=10,
    )
    assert rejected.status_code == 200 and rejected.json()["patient_visible"] is False

    # 7. Patient result — the decision, visible.
    final = {i["record_id"]: i for i in _summary(patient)}

    released = final[release["record_id"]]
    stored = _rows("SELECT body FROM records WHERE id=%s", (release["record_id"],))[0][0]
    assert released["quote"] == stored.strip(), "released verbatim, not rewritten"
    assert released["released_by_review"] is True, "and the patient is told a person decided"

    still_withheld = final[withhold["record_id"]]
    assert still_withheld["quote"] is None
    assert still_withheld["refusal_reason"]


# --------------------------------------------------------------------------
# The client's negative list. A passing happy path proves nothing without these.
# --------------------------------------------------------------------------


def test_negative_patient_a_cannot_see_patient_b(clean):
    """Criterion: Patient A cannot see Patient B."""
    staff = _token(FRONT_DESK)
    a = _activate(PATIENT_A, staff)
    _activate(PATIENT_B, staff)

    # Every route that could name another patient.
    for path in (
        f"/patients/{PATIENT_B}",
        f"/patients/{PATIENT_B}/records",
        f"/patients/{PATIENT_B}/view",
        f"/patients/{PATIENT_B}/reconciliation",
    ):
        r = httpx.get(f"{GATEWAY}{path}", headers=_auth(a), timeout=10)
        assert r.status_code in (401, 403), f"{path} leaked to another patient ({r.status_code})"

    # And the one route A *can* call returns only A.
    assert all(
        i["record_id"] in {r[0] for r in _rows("SELECT id FROM records WHERE patient_id=%s", (PATIENT_A,))}
        for i in _summary(a)
    ), "the summary must contain only this patient's own records"


def test_negative_a_patient_cannot_reach_the_raw_clinician_chart_route(clean):
    """Criterion: patient cannot access the raw clinician chart route.

    Including for their OWN chart — the patient surface is the summary, which
    applies the content rules. The raw route returns record bodies unfiltered.
    """
    a = _activate(PATIENT_A, _token(FRONT_DESK))

    for path in (f"/patients/{PATIENT_A}/records", f"/patients/{PATIENT_A}/view", "/records/search?q=a"):
        r = httpx.get(f"{GATEWAY}{path}", headers=_auth(a), timeout=10)
        assert r.status_code in (401, 403), f"{path} should be staff-only ({r.status_code})"


def test_negative_a_multi_value_panel_produces_no_inferred_delta(clean):
    """Criterion: a multi-value panel does not produce an inferred delta.

    The panel still shows its numbers — withholding it wholesale is the
    over-refusal the client ruled out by name. Only the delta refuses.
    """
    a = _activate(PATIENT_A, _token(FRONT_DESK))
    panels = [i for i in _summary(a) if i["shape"] == "panel"]
    assert panels, "the fixture chart must contain a panel"

    for panel in panels:
        assert panel["quote"], "a panel is quoted, not withheld"
        assert panel["change"] is None, "but never carries a computed change"
        assert panel["refusal_reason"] is None


def test_negative_unsupported_content_refuses(clean):
    """Criterion: unsupported content refuses rather than guessing."""
    a = _activate(PATIENT_A, _token(FRONT_DESK))
    refused = [i for i in _summary(a) if i["shape"] == "unquotable"]
    assert refused

    for item in refused:
        assert item["quote"] is None
        assert item["change"] is None
        assert item["refusal_reason"], "and says so, rather than showing an empty result"


def test_negative_a_rejected_review_does_not_become_patient_visible(clean):
    """Criterion: a rejected review never becomes patient-visible.

    Asserted through the full stack, and then again after a fresh read, because
    the failure mode worth fearing is a rejection that decays into a disclosure
    on the next page load.
    """
    staff, clinician = _token(FRONT_DESK), _token(CLINICIAN)
    a = _activate(PATIENT_A, staff)
    _summary(a)   # queues the refusals

    case = [
        c
        for c in httpx.get(f"{GATEWAY}/review-queue", headers=_auth(clinician), timeout=10).json()[
            "items"
        ]
        if c["patient_id"] == PATIENT_A
    ][0]

    httpx.post(
        f"{GATEWAY}/review-queue/{case['id']}/decision",
        headers=_auth(clinician),
        json={"decision": "rejected"},
        timeout=10,
    )

    for _ in range(3):
        shown = {i["record_id"]: i for i in _summary(a)}[case["record_id"]]
        assert shown["quote"] is None, "a rejection must not decay into a disclosure"
        assert shown["released_by_review"] is False

    assert _rows(
        "SELECT count(*) FROM patient_summary_reviews WHERE record_id=%s", (case["record_id"],)
    )[0][0] == 1, "and must not be re-queued for another clinician to overturn by accident"


def test_negative_every_source_link_resolves_to_the_record_it_claims(clean):
    """Criterion: source links correspond to actual source records.

    Every figure shown must be traceable. A link to a record that does not
    exist, or belongs to someone else, would make the citation worse than
    useless — it would look like provenance while providing none.
    """
    a = _activate(PATIENT_A, _token(FRONT_DESK))
    items = _summary(a)

    owned = {r[0] for r in _rows("SELECT id FROM records WHERE patient_id=%s", (PATIENT_A,))}
    assert owned

    for item in items:
        assert item["source_record_ids"], "every item cites at least its own record"
        for rid in item["source_record_ids"]:
            assert rid in owned, f"source {rid} is not a record of patient {PATIENT_A}"

        change = item.get("change")
        if change:
            assert change["from_record_id"] in owned
            assert change["from_record_id"] in item["source_record_ids"], (
                "a computed change must cite the record it was measured against"
            )
