"""W10 Final 2 Stage 4 — bounded full-stack startup and integration
verification. Requires the FULL stack up (`make up`, not just Postgres) on
a disposable environment (see .github/workflows/ci.yml's
`full-stack-verification` job) that has already run `make demo-reset`.

This is deliberately the smallest representative walk across every
service's contract with the gateway, not the historical integration suite —
selected to prove cross-service startup/wiring works end to end without
excessive runtime or flaky external dependencies:

  login/session -> patient-scoped chart read -> booking -> patient-summary
  request/review/approved display -> messaging -> corrected ROI
  (create -> authorize -> review -> fulfill).

No paid or real payer/provider call is made anywhere in this walk:
- patient-summary request/review/display uses GET /patient/me/summary,
  which is deterministic quote-rendering with "no model call anywhere
  beneath it" (see services/records-service/app.py's own comment on that
  route) — the refusal cases it produces are exactly what populates the
  review queue, the same mechanism tests/integration/test_demo_reset.py's
  own _walk_to_a_populated_queue already relies on.
- coverage/eligibility defaults to PAYER_INTEGRATION_MODE=simulation
  (services/eligibility-service/config.py) when unset, as it is in this
  job's environment — this file does not touch that path at all, so there
  is nothing to accidentally call live.
- messaging and ROI are ordinary CRUD/authorization paths with no AI or
  external-network step.

Run with:  pytest -m integration tests/integration/test_full_stack_smoke.py
"""
import os
import time

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")

# 1738: pre-activated, drkim+drnguyen overlap, seeded thread id 1.
DEMO_PATIENT = 1738
REVIEWER = "drkim"
PATIENT_PASSWORD = "portalportal123"  # db/seed/generate_seed.py's PATIENT_DEMO_PASSWORD


def _token(username, password="portal123"):
    # Retries only on a transient 503 — never on a real auth failure. A
    # resource-constrained CI runner starting the whole stack at once can
    # leave a service's underlying DB connection settling for a few seconds
    # after its own /healthz already reports ready (see .github/workflows/
    # ci.yml's "Warm up" step, which checks Postgres itself but can't prove
    # every app service's OWN connection is past that same window). Bounded
    # at 5 attempts specifically so this can never approach the gateway's
    # own login rate limit (10 attempts per username per 5 minutes).
    last_response = None
    for attempt in range(5):
        r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": password}, timeout=10)
        if r.status_code != 503:
            r.raise_for_status()
            return r.json()["token"]
        last_response = r
        time.sleep(2)
    last_response.raise_for_status()


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_minimal_authorized_path_across_every_service():
    # --- login/session ------------------------------------------------------
    frontdesk = _token("frontdesk")
    reviewer = _token(REVIEWER)
    patient = _token(f"patient-{DEMO_PATIENT}", PATIENT_PASSWORD)

    # --- patient-scoped chart read (records-service) ------------------------
    records = httpx.get(f"{GATEWAY}/patients/{DEMO_PATIENT}/records", headers=_auth(reviewer), timeout=10)
    assert records.status_code == 200, records.text
    assert records.json()["encounters"], "the seeded chart must have at least one encounter"

    # --- booking (scheduling-service) ---------------------------------------
    slots = httpx.get(f"{GATEWAY}/slots?limit=20", headers=_auth(frontdesk), timeout=10).json()["items"]
    open_slot = next(
        s for s in slots if 95001 <= s["id"] <= 95016 and s["status"] not in ("booked", "cancelled")
    )
    booking = httpx.post(
        f"{GATEWAY}/appointments",
        headers=_auth(frontdesk),
        json={"patient_id": DEMO_PATIENT, "slot_id": open_slot["id"],
              "idempotency_key": "full-stack-smoke-booking"},
        timeout=10,
    )
    assert booking.status_code == 201, booking.text

    # --- patient-summary request / review / approved display ---------------
    # No model call anywhere on this route (see module docstring) — a refusal
    # here is the expected, deterministic way the review queue populates.
    own_summary = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient), timeout=10)
    assert own_summary.status_code == 200, own_summary.text

    queue = httpx.get(f"{GATEWAY}/review-queue", headers=_auth(reviewer), timeout=10)
    assert queue.status_code == 200, queue.text
    cases = [c for c in queue.json()["items"] if c["patient_id"] == DEMO_PATIENT]
    assert cases, "the deterministic refusal path must have queued at least one case for review"

    decision = httpx.post(
        f"{GATEWAY}/review-queue/{cases[0]['id']}/decision",
        headers=_auth(reviewer), json={"decision": "approved"}, timeout=10,
    )
    assert decision.status_code == 200, decision.text

    approved_display = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient), timeout=10)
    assert approved_display.status_code == 200, approved_display.text

    # --- messaging (thread 1 is seeded for patient 1738) --------------------
    reply = httpx.post(
        f"{GATEWAY}/threads/1/messages", headers=_auth(reviewer),
        json={"body": "Full-stack smoke test reply.", "idempotency_key": "full-stack-smoke-reply"},
        timeout=10,
    )
    assert reply.status_code == 201, reply.text
    thread = httpx.get(f"{GATEWAY}/threads/1", headers=_auth(reviewer), timeout=10)
    assert thread.status_code == 200, thread.text
    assert any(m["body"] == "Full-stack smoke test reply." for m in thread.json()["messages"])

    # --- corrected ROI: create -> authorize -> review -> fulfill ------------
    # Exercises W10 Final 2 Stage 1's trust boundary end to end: trusted
    # actor identity (requested_by/reviewed_by are derived from the session,
    # never these bodies), the patient-grant check (frontdesk holds one for
    # every canonical patient), and status-code propagation.
    roi_request = httpx.post(
        f"{GATEWAY}/roi/requests", headers=_auth(frontdesk),
        json={"patient_id": DEMO_PATIENT, "recipient": "Full-Stack Smoke Test Recipient",
              "recipient_type": "provider", "purpose": "continuity of care"},
        timeout=10,
    )
    assert roi_request.status_code == 201, roi_request.text
    request_id = roi_request.json()["id"]

    authorization = httpx.post(
        f"{GATEWAY}/roi/authorizations", headers=_auth(frontdesk),
        json={"patient_id": DEMO_PATIENT, "recipient": "Full-Stack Smoke Test Recipient",
              "purpose": "continuity of care", "signature_evidence_reference": "full-stack-smoke-signed-form",
              "signed_by": "Smoke Test Patient", "signed_at": "2026-01-01T00:00:00Z"},
        timeout=10,
    )
    assert authorization.status_code == 201, authorization.text
    auth_id = authorization.json()["id"]

    review = httpx.post(
        f"{GATEWAY}/roi/authorizations/{auth_id}/review", headers=_auth(frontdesk),
        json={"decision": "valid", "reviewed_by": "full-stack-smoke-reviewer"}, timeout=10,
    )
    assert review.status_code == 200, review.text

    fulfill = httpx.post(
        f"{GATEWAY}/roi/requests/{request_id}/fulfill", headers=_auth(frontdesk),
        json={"authorization_id": auth_id}, timeout=10,
    )
    assert fulfill.status_code == 200, fulfill.text
    assert fulfill.json()["disclosure_id"], "fulfillment must produce a real disclosure accounting row"
