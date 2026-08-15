"""Integration tests — require the full stack up (`make up`) on localhost.

The patient's own view, end to end: invitation -> activation -> sign-in ->
reading exactly one chart.

This is the test the adversarial review of #34/#35 asked for by name. Those
PRs shipped `own_record.read` with no route accepting it, so an activated
patient could authenticate and reach nothing; the assertions here are what
prove that gap is actually closed rather than merely coded around.

Two directions matter equally and both are asserted:
  * a patient reaches their OWN results, and
  * that same patient is refused everyone else's chart, and staff are refused
    the patient route.

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

# Seeded demo patients (db/seed/generate_seed.py) — real rows. No PHI invented.
#
# 1737 is chosen deliberately: its chart exercises all three content outcomes
# in one patient — a panel ("Na 140, K 4.1, Cr 0.9."), a single value repeated
# across two encounters ("2.3 mIU/L." twice, so a change can be computed), and
# visit notes that must refuse. Patient 1042 has no lab results at all, so the
# content assertions below silently skipped against it and proved nothing.
_PATIENT_A = 1737
_PATIENT_B = 1629

_PASSWORD = "portal-patient-passphrase"   # over the 12-char activation floor


def _token(username: str, password: str = "portal123") -> str:
    r = httpx.post(
        f"{GATEWAY}/login", json={"username": username, "password": password}, timeout=10
    )
    r.raise_for_status()
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sql(statement: str, params=()) -> None:
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(statement, params)
        conn.commit()


def _reset_patient_account(patient_id: int) -> None:
    """Remove any account/invitation left by a previous run.

    Grants are deleted through the account, so the patient is back to having
    no portal access at all — otherwise the second run of this file would be
    reading through state the first run created.
    """
    _sql(
        "DELETE FROM patient_access_grants WHERE user_id IN"
        " (SELECT id FROM users WHERE patient_id = %s)",
        (patient_id,),
    )
    _sql("DELETE FROM patient_invitations WHERE patient_id = %s", (patient_id,))
    _sql("DELETE FROM users WHERE patient_id = %s", (patient_id,))


def _activate_patient(patient_id: int, staff_token: str) -> str:
    """Issue an invitation as front desk, redeem it, return the patient's token."""
    issued = httpx.post(
        f"{GATEWAY}/patients/{patient_id}/invitation", headers=_auth(staff_token), timeout=10
    )
    assert issued.status_code == 201, issued.text
    code = issued.json()["code"]

    activated = httpx.post(
        f"{GATEWAY}/patient/activate", json={"code": code, "password": _PASSWORD}, timeout=10
    )
    assert activated.status_code == 200, activated.text

    return _token(f"patient-{patient_id}", _PASSWORD)


@pytest.fixture
def patient_a_token():
    _reset_patient_account(_PATIENT_A)
    token = _activate_patient(_PATIENT_A, _token("frontdesk"))
    yield token
    _reset_patient_account(_PATIENT_A)


def test_an_activated_patient_can_read_their_own_results(patient_a_token):
    """The gap the review found: activation used to hand out credentials that
    reached nothing."""
    r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient_a_token), timeout=10)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient_id"] == _PATIENT_A
    assert isinstance(body["items"], list)


def test_the_summary_never_contains_a_raw_record_body_field(patient_a_token):
    """The response carries quotes, not bodies. A client cannot render prose
    the content rules withheld if it is never sent."""
    r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient_a_token), timeout=10)

    for item in r.json()["items"]:
        assert "body" not in item
        # quote and refusal are mutually exclusive, per the content rules
        assert (item.get("quote") is None) != (item.get("refusal_reason") is None)


def test_quoted_values_are_verbatim_from_the_stored_result(patient_a_token):
    """Every quote shown must match the stored body exactly — this is the
    whole promise of the feature, so it is checked against the database
    rather than against our own rendering."""
    r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient_a_token), timeout=10)
    items = {i["record_id"]: i for i in r.json()["items"] if i.get("quote")}
    # Asserted, not skipped: a skip here would hide the feature silently
    # producing nothing at all.
    assert items, "the fixture patient must have quotable results for this to mean anything"

    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, body, reference_range FROM records WHERE id = ANY(%s)",
            (list(items),),
        )
        for record_id, body, reference_range in cur.fetchall():
            assert items[record_id]["quote"] == (body or "").strip(), (
                f"record {record_id} was not quoted verbatim"
            )
            # A range is shown exactly as printed, or not at all — never
            # synthesized.
            shown = items[record_id].get("reference_range")
            assert shown == ((reference_range or "").strip() or None)


def test_a_panel_result_still_shows_its_values(patient_a_token):
    """Guards the over-refusal the client called out by name."""
    r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient_a_token), timeout=10)
    panels = [i for i in r.json()["items"] if i["shape"] == "panel"]
    assert panels, "the fixture patient must have a panel result for this to mean anything"

    for panel in panels:
        assert panel["quote"], "a panel must still be quoted"
        assert panel["change"] is None, "a panel must never carry a computed change"
        assert panel["refusal_reason"] is None, "a panel must not be withheld wholesale"


def test_a_patient_cannot_read_another_patients_chart(patient_a_token):
    """The IDOR direction. A patient holds exactly one grant, and no staff
    permission at all, so every staff chart route must refuse them."""
    for path in (
        f"/patients/{_PATIENT_B}/records",
        f"/patients/{_PATIENT_B}",
        f"/patients/{_PATIENT_B}/view",
        f"/patients/{_PATIENT_A}/records",   # their OWN chart, via the staff route
    ):
        r = httpx.get(f"{GATEWAY}{path}", headers=_auth(patient_a_token), timeout=10)
        assert r.status_code in (401, 403), f"{path} should refuse a patient, got {r.status_code}"


def test_staff_cannot_read_the_patient_route(patient_a_token):
    """The reverse direction, and the reason own_record.read is its own
    permission: a clinician reading a chart goes through the staff routes,
    where the access is audited as staff access. If staff could call this,
    the two would collapse into one audit line."""
    for account in ("frontdesk", "clinician"):
        try:
            staff = _token(account)
        except httpx.HTTPStatusError:
            continue  # not every demo account exists in every seed
        r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(staff), timeout=10)
        assert r.status_code == 403, f"{account} should not reach the patient route"


def test_the_patient_route_refuses_an_unauthenticated_caller():
    r = httpx.get(f"{GATEWAY}/patient/me/summary", timeout=10)
    assert r.status_code in (401, 403)


def test_a_revoked_grant_closes_the_patients_own_view(patient_a_token):
    """Access is the grant, not the account. Revoking the grant must close
    the view even though the login still works — otherwise revocation is
    cosmetic."""
    ok = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient_a_token), timeout=10)
    assert ok.status_code == 200

    _sql(
        "UPDATE patient_access_grants SET revoked_at = now()"
        " WHERE user_id IN (SELECT id FROM users WHERE patient_id = %s)",
        (_PATIENT_A,),
    )

    after = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient_a_token), timeout=10)
    assert after.status_code == 403, "a revoked grant must close the view"


def test_a_repeated_single_value_carries_a_change_linked_to_its_source(patient_a_token):
    """Patient 1737 has TSH "2.3 mIU/L." recorded at two encounters, so the
    later one must carry a change measured against the earlier — and must name
    the record it was measured against, which is the "link to its source"
    requirement.
    """
    r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient_a_token), timeout=10)
    items = r.json()["items"]

    with_change = [i for i in items if i.get("change")]
    assert with_change, "a repeated single-value result must produce a change"

    for item in with_change:
        change = item["change"]
        assert item["shape"] == "single_value", "only a single value may carry a change"
        assert change["direction"] in ("up", "down", "unchanged")
        assert change["from_record_id"] in {i["record_id"] for i in items}, (
            "a change must link to a record the patient can actually see"
        )
        assert change["from_record_id"] in item["source_record_ids"]


def test_a_note_is_refused_rather_than_quoted(patient_a_token):
    """The third outcome, against real data: this patient's visit notes
    ("Labs reviewed.", "Patient stable.") carry no quotable measurement."""
    r = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient_a_token), timeout=10)
    refused = [i for i in r.json()["items"] if i["shape"] == "unquotable"]

    assert refused, "the fixture patient must have a prose record for this to mean anything"
    for item in refused:
        assert item["quote"] is None
        assert item["refusal_reason"]
        assert item["change"] is None
