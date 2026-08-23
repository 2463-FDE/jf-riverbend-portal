"""Integration tests — require the full stack up (`make up`) on localhost.

Branches 7A and 7B: all four previously-unverified domain services —
eligibility, scheduling, interop and roi — verify their callers.

Unpublishing their host ports (#39) was containment — it stopped anything on
the host reaching them, but anything already inside the compose network was
still trusted blind and could supply whatever it liked. This asserts the
actual check: a call without the shared token is refused even from inside the
network, while the gateway's own calls keep working.

Both directions matter. A change that only proved the refusal would also pass
if the services had simply stopped working, which is the failure this suite
has to be able to tell apart.

Run with:  pytest -m integration
"""
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

# Every domain service that verifies the shared token, with one guarded route
# each. interop and roi were added in 7B.
#
# roi's entry is /disclosures/1042 deliberately: it releases a patient's records
# and has no gateway route at all, so before 7B its only reachable caller was an
# unauthenticated direct one. If any single route in this dict has to keep
# working, it is that one.
_GUARDED = {
    "eligibility-service": (8072, "/eligibility?insurance_id=1"),
    "scheduling-service": (8074, "/slots"),
    "interop-service": (8075, "/hl7/sample"),
    "roi-service": (8076, "/disclosures/1042"),
}


def _token(username="frontdesk", password="portal123"):
    r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def _rejected(response) -> bool:
    """The gateway wraps a downstream 401 in its own 200 for non-forwarding
    proxies, so a status check alone would report success on a rejected call.
    This looks at what actually came back — a distinction that cost a
    debugging cycle when the token was configured for three services and not
    the two being added."""
    return "internal service token" in response.text


@pytest.fixture
def staff_token():
    return _token()


# --- the check itself ------------------------------------------------------


@pytest.mark.parametrize("service", sorted(_GUARDED))
def test_an_in_network_caller_without_the_token_is_refused(service):
    """The part unpublishing the port does not give you.

    Driven from inside the compose network via the gateway container, because
    that is the threat this closes: something already on the network calling a
    domain service directly and being trusted.
    """
    port, path = _GUARDED[service]
    import subprocess

    script = (
        "import urllib.request, urllib.error, sys\n"
        f"try:\n"
        f"    urllib.request.urlopen('http://{service}:{port}{path}', timeout=5)\n"
        f"    print('ALLOWED')\n"
        f"except urllib.error.HTTPError as e:\n"
        f"    print(e.code)\n"
    )
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "gateway", "python", "-c", script],
        capture_output=True, text=True, cwd=os.getcwd(),
    ).stdout.strip()

    if not out:
        pytest.skip("could not reach the compose network from this test runner")
    assert out == "401", f"{service} accepted an untokened in-network call ({out})"


# --- and the flows still work ----------------------------------------------


def test_slot_search_still_works_through_the_gateway(staff_token):
    r = httpx.get(f"{GATEWAY}/slots?limit=3", headers=_auth(staff_token), timeout=25)
    assert r.status_code == 200 and not _rejected(r), r.text
    assert "items" in r.json()


def test_appointment_listing_still_works_through_the_gateway(staff_token):
    r = httpx.get(f"{GATEWAY}/appointments?patient_id=1042", headers=_auth(staff_token), timeout=25)
    assert r.status_code == 200 and not _rejected(r), r.text


def test_eligibility_still_works_through_the_gateway(staff_token):
    r = httpx.get(f"{GATEWAY}/eligibility?insurance_id=1", headers=_auth(staff_token), timeout=25)
    assert r.status_code == 200 and not _rejected(r), r.text


def test_roi_listing_still_works_through_the_gateway(staff_token):
    """7B. The gateway injects the token in _internal_headers for every
    outbound call, so this needed no gateway change — which is exactly why it
    has to be proven rather than assumed.

    _rejected matters more here than status does: proxy_roi_list does not
    forward the downstream status, so a 401 from roi-service would arrive as a
    gateway 200 with the refusal in the body. A status-only assertion would
    call that a pass.
    """
    r = httpx.get(f"{GATEWAY}/roi/requests?patient_id=1042", headers=_auth(staff_token), timeout=25)
    assert r.status_code == 200 and not _rejected(r), r.text


def test_hl7_ingest_still_works_through_the_gateway(staff_token):
    """7B, and a write — the token has to be on the POST helper too."""
    r = httpx.post(
        f"{GATEWAY}/hl7/ingest",
        headers=_auth(staff_token),
        json={"message": "MSH|^~\\&|HOSP|RIVERBEND|||20260820||ADT^A01|1|P|2.3\rPID|||1042||Alvarez^Maria"},
        timeout=25,
    )
    assert r.status_code == 200 and not _rejected(r), r.text


def test_booking_still_works_through_the_gateway(staff_token):
    """A write, not just a read — the token has to be on POST as well as GET,
    and those go through different helpers in the gateway.

    w9-fixes P0 4.2/4.3: this used to book a synthetic, out-of-range slot_id
    (random.randint(900_000, 999_999)). That worked only because booking
    never checked the slot actually existed and was open — book.py's
    _lock_open_slot now requires both (and a future start_at), so a
    synthetic id is refused the same way a genuinely taken slot is. What
    this test is actually about (the internal-service-token reaching the
    gateway's POST helper) needs a real, throwaway slot instead."""
    start = datetime.utcnow() + timedelta(days=3)
    with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slots (provider_id, location, start_at, end_at, status) "
            "VALUES (1, 'Riverbend Main', %s, %s, 'open') RETURNING id",
            (start, start + timedelta(minutes=30)),
        )
        slot_id = cur.fetchone()[0]
        conn.commit()

    try:
        r = httpx.post(
            f"{GATEWAY}/appointments",
            headers=_auth(staff_token),
            json={
                "patient_id": 1042,
                "slot_id": slot_id,
                "idempotency_key": str(uuid.uuid4()),
            },
            timeout=25,
        )
        assert r.status_code == 201 and not _rejected(r), r.text
    finally:
        with psycopg2.connect(DB_DSN) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM appointments WHERE slot_id = %s", (slot_id,))
            cur.execute("DELETE FROM slots WHERE id = %s", (slot_id,))
            conn.commit()


def test_healthz_stays_reachable_without_a_token():
    """Deliberately unguarded: compose's healthcheck calls it from inside the
    container, and a health probe that needed the app secret would turn a
    token misconfiguration into an unexplained unhealthy container instead of
    a clear 401 on real traffic."""
    import subprocess

    for service, (port, _) in _GUARDED.items():
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", service, "python", "-c",
             f"import urllib.request; print(urllib.request.urlopen('http://localhost:{port}/healthz', timeout=5).status)"],
            capture_output=True, text=True, cwd=os.getcwd(),
        ).stdout.strip()
        if not out:
            pytest.skip("could not exec into the service container")
        assert out == "200", f"{service} /healthz should not require the token ({out})"
