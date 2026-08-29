"""Integration test — requires a real Postgres (`make up`, or the CI
concurrency job's disposable postgres-only container).

W10 Final Stage 2: proves fulfill_roi_request's remaining concurrent-
duplicate-fulfillment gap is actually closed under real concurrency —
two genuinely simultaneous fulfillment attempts for the SAME roi_request
must produce exactly one disclosure row. Calls services/roi-service/
app.py::fulfill_roi_request directly (no HTTP, no gateway) with two
independent real database sessions, the same "connect directly to
Postgres" pattern as test_agent_draft_provenance_contract.py — this
deliverable's whole contract lives in a row lock plus migration 035's
unique index, not in a network layer.

Every row this file creates is a fresh, uuid-suffixed throwaway (own
patient, own authorization, own request) — never touches seeded/shared
data, and needs no cleanup beyond the test transaction's own commits
(mirrors test_agent_draft_provenance_contract.py's own convention).

Run with:  pytest -m integration tests/integration/test_roi_fulfillment_concurrency.py
Skipped by default in CI's main `tests` job; wired into a dedicated
concurrency CI job instead.
"""
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from conftest import load_module  # noqa: E402

pytestmark = pytest.mark.integration

os.environ.setdefault("DB_HOST", "localhost")

app_mod = load_module("services/roi-service/app.py", "roi_concurrency_integration_app")
Patient = app_mod.Patient
RoiAuthorization = app_mod.RoiAuthorization
RoiRequest = app_mod.RoiRequest
Disclosure = app_mod.Disclosure
FulfillRequest = app_mod.FulfillRequest


@pytest.fixture
def session_factory():
    # app.py imports only `get_db` by name from db.py; the sessionmaker
    # bound to the SAME engine/settings db.py itself resolved lives in that
    # function's own module globals.
    return app_mod.get_db.__globals__["get_sessionmaker"]()


@pytest.fixture
def patient_id(session_factory):
    with session_factory() as s:
        p = Patient(name=f"ROI Concurrency Test {uuid.uuid4().hex[:8]}")
        s.add(p)
        s.commit()
        s.refresh(p)
        return p.id


@pytest.fixture
def authorization_id(session_factory, patient_id):
    now = datetime.now(timezone.utc)
    with session_factory() as s:
        auth = RoiAuthorization(
            patient_id=patient_id, recipient="Dr. Concurrency Test",
            signature_evidence_reference=f"ref-{uuid.uuid4().hex[:8]}",
            signed_by="Test Patient", signed_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=365),
            status="valid", reviewed_by="supervisor-test", reviewed_at=now,
        )
        s.add(auth)
        s.commit()
        s.refresh(auth)
        return auth.id


@pytest.fixture
def request_id(session_factory, patient_id):
    with session_factory() as s:
        req = RoiRequest(
            patient_id=patient_id, requested_by="front-desk-test",
            recipient="Dr. Concurrency Test", recipient_type="provider",
            purpose="continuity of care", status="pending",
        )
        s.add(req)
        s.commit()
        s.refresh(req)
        return req.id


def test_two_simultaneous_fulfillments_of_the_same_request_yield_one_disclosure(
    session_factory, patient_id, authorization_id, request_id
):
    from fastapi import HTTPException

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def _attempt():
        db = session_factory()
        try:
            barrier.wait(timeout=5)
            try:
                result = app_mod.fulfill_roi_request(
                    request_id, FulfillRequest(authorization_id=authorization_id), db=db
                )
                with lock:
                    outcomes.append(("ok", result.disclosure_id))
            except HTTPException as exc:
                with lock:
                    outcomes.append(("denied", exc.status_code))
        finally:
            db.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(outcomes) == 2
    successes = [o for o in outcomes if o[0] == "ok"]
    denials = [o for o in outcomes if o[0] == "denied"]
    assert len(successes) == 1, f"exactly one fulfillment must win: {outcomes}"
    assert len(denials) == 1 and denials[0][1] == 409, f"the loser must get a truthful 409: {outcomes}"

    with session_factory() as s:
        count = s.query(Disclosure).filter_by(roi_request_id=request_id).count()
    assert count == 1, "concurrent requests must never create more than one disclosure effect"
