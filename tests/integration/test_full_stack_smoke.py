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
  (create -> authorize -> review -> fulfill) -> synthetic intake ->
  simulated coverage/eligibility -> synthetic HL7 ingest -> frontend
  reachability.

No paid or real payer/provider call is made anywhere in this walk:
- patient-summary request/review/display uses GET /patient/me/summary,
  which is deterministic quote-rendering with "no model call anywhere
  beneath it" (see services/records-service/app.py's own comment on that
  route) — the refusal cases it produces are exactly what populates the
  review queue, the same mechanism tests/integration/test_demo_reset.py's
  own _walk_to_a_populated_queue already relies on.
- coverage/eligibility verification is exercised explicitly in
  PAYER_INTEGRATION_MODE=simulation (the default when unset, as it is in
  this job's environment) — gateway's own verify_patient_coverage takes
  that branch before eligibility-service is ever contacted (see
  services/gateway/app.py's own comment on that boundary), so this proves
  the simulation path specifically, not merely "coverage wasn't touched".
- HL7 ingest is a pure parse — interop-service never calls anything
  external; it uses the repo's own bundled ADT sample
  (services/interop-service/samples/adt_sample.hl7), the same fixture that
  service's own /hl7/sample route exists to hand out for smoke-testing.
- intake creates one synthetic patient (obviously-fake SSN/DOB, no
  insurance) with no clinician-review or AI step on that path.
- messaging and ROI are ordinary CRUD/authorization paths with no AI or
  external-network step.

Run with:  pytest -m integration tests/integration/test_full_stack_smoke.py
"""
import os
import pathlib

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8070")
FRONTEND = os.getenv("FRONTEND_URL", "http://localhost:3070")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# 1738: pre-activated, drkim+drnguyen overlap, seeded thread id 1.
DEMO_PATIENT = 1738
REVIEWER = "drkim"
PATIENT_PASSWORD = "portalportal123"  # db/seed/generate_seed.py's PATIENT_DEMO_PASSWORD


def _token(username, password="portal123"):
    r = httpx.post(f"{GATEWAY}/login", json={"username": username, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


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
    approved_record_id = cases[0]["record_id"]

    decision = httpx.post(
        f"{GATEWAY}/review-queue/{cases[0]['id']}/decision",
        headers=_auth(reviewer), json={"decision": "approved"}, timeout=10,
    )
    assert decision.status_code == 200, decision.text

    approved_display = httpx.get(f"{GATEWAY}/patient/me/summary", headers=_auth(patient), timeout=10)
    assert approved_display.status_code == 200, approved_display.text
    approved_item = next(
        (item for item in approved_display.json()["items"] if item["record_id"] == approved_record_id), None
    )
    assert approved_item is not None, "the approved record must still appear in the patient's own summary"
    assert approved_item["released_by_review"] is True, (
        "approval must mark this item released_by_review — the patient must be told this content "
        "was clinician-released, not shown text indistinguishable from a system-quoted result"
    )
    assert approved_item["quote"], "an approved item must carry the released content, not remain refused"

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
        # Caller-supplied reviewed_by — must be ignored. frontdesk is the
        # session actually authenticated, and only that identity may end up
        # in the persisted/returned row (W10 Final 2 Stage 1's trust
        # boundary: requested_by/reviewed_by/revoked_by are always derived
        # from the session, never the request body).
        json={"decision": "valid", "reviewed_by": "someone-else-entirely"}, timeout=10,
    )
    assert review.status_code == 200, review.text
    assert review.json()["reviewed_by"] == "frontdesk", (
        f"reviewed_by must record the authenticated actor (frontdesk), not the caller-supplied "
        f"value — got {review.json()['reviewed_by']!r}"
    )

    fulfill = httpx.post(
        f"{GATEWAY}/roi/requests/{request_id}/fulfill", headers=_auth(frontdesk),
        json={"authorization_id": auth_id}, timeout=10,
    )
    assert fulfill.status_code == 200, fulfill.text
    assert fulfill.json()["disclosure_id"], "fulfillment must produce a real disclosure accounting row"

    # --- synthetic intake (intake-service) -----------------------------------
    # Obviously-fake SSN (900-series is never issued to a real person) and a
    # fixed, clearly-synthetic DOB — no insurance, so no eligibility job is
    # queued by this call (that path is exercised separately below, in
    # simulation mode, against an already-seeded coverage instead).
    intake = httpx.post(
        f"{GATEWAY}/intake", headers=_auth(frontdesk),
        json={
            "demographics": {
                "first_name": "FullStackSmoke", "last_name": "TestPatient",
                "dob": "1990-01-01", "ssn": "900-00-0001", "gender": "other",
            },
            "consents": ["npp_ack", "treatment_consent"],
        },
        timeout=10,
    )
    assert intake.status_code == 201, intake.text
    assert intake.json()["patient_id"], "intake must create and return a real patient_id"

    # --- coverage/eligibility, explicit simulation mode (eligibility-service) -
    coverages = httpx.get(f"{GATEWAY}/patients/{DEMO_PATIENT}/coverages", headers=_auth(frontdesk), timeout=10)
    assert coverages.status_code == 200, coverages.text
    coverage = next(c for c in coverages.json()["items"] if c["has_member_id"])

    verify = httpx.post(
        f"{GATEWAY}/patients/{DEMO_PATIENT}/coverages/{coverage['id']}/verify",
        headers=_auth(frontdesk), timeout=10,
    )
    assert verify.status_code == 201, verify.text
    assert verify.json() == {
        "category": "simulated", "message": "Synthetic training — no payer contacted", "can_retry": False,
    }, "PAYER_INTEGRATION_MODE=simulation must make zero outbound calls, taken before eligibility-service is ever contacted"

    # --- synthetic HL7 ingest (interop-service) ------------------------------
    sample_message = (REPO_ROOT / "services" / "interop-service" / "samples" / "adt_sample.hl7").read_text()
    hl7 = httpx.post(f"{GATEWAY}/hl7/ingest", headers=_auth(reviewer), json={"message": sample_message}, timeout=10)
    assert hl7.status_code == 200, hl7.text
    hl7_body = hl7.json()
    assert hl7_body["record"]["mrn"].startswith("M4471")
    assert "Gonzalez" in hl7_body["record"]["name"]
    assert hl7_body["record"]["allergies"], "the sample's AL1 segment must map to a real allergy entry"
    assert hl7_body["record"]["medications"], "the sample's RXA segment must map to a real medication entry"
    assert hl7_body["has_incomplete_content"] is False, "the bundled sample is a known-complete message"

    # --- frontend reachability ------------------------------------------------
    frontend_page = httpx.get(FRONTEND, timeout=10)
    assert frontend_page.status_code == 200, frontend_page.text
    assert "Riverbend" in frontend_page.text, "the served page must be the actual portal, not an empty/error shell"
