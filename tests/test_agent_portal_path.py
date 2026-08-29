"""The portal path: generate -> clinician review -> approved-only display.

Drives the real records-service routes against a real SQLite session, so the
authorization these tests are about is the actual one — the permission lookup
and the per-(actor, patient) grant query both run.

The model is never involved: drafts are created through the merged write path
with a `fixture` label, because what is under test here is who may see which
version, not what the agent writes.
"""
import base64
import itertools
import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module, phi_globals_of
from libs.phi_crypto import EnvKeyProvider

app_mod = load_module("services/records-service/app.py", "records_app_agent_portal")
drafts = app_mod.agent_drafts
# PatientAccessGrant is not imported into app.py; reach it through the models
# module app.py itself bound, so this fixture builds rows in the same metadata.
models = sys.modules[app_mod.AgentDraftProvenance.__module__]

# adr/0012 follow-up (agent draft text encryption): app.py's _draft_out
# decrypts, and agent_drafts.py's create_draft encrypts — needs a
# configured PHI key provider on BOTH sides. See
# conftest.phi_globals_of's docstring for why patching sys.modules["phi"]
# directly is not reliable when two loaded siblings (app.py's own phi
# binding and agent_drafts.py's, reached via app_mod.agent_drafts.phi) can
# each end up bound to a different phi.py module instance. drafts.phi is
# unambiguous on its own (agent_drafts.py's plain `import phi` binds the
# module object directly), patched alongside app_mod's own binding to be
# safe regardless of which one(s) any given call actually goes through.
_TEST_PHI_PROVIDER = EnvKeyProvider(
    {
        "PHI_ACTIVE_KEY_VERSION": "v1",
        "PHI_ENCRYPTION_KEY_V1": base64.b64encode(os.urandom(32)).decode(),
        "PHI_BLIND_INDEX_KEY_V1": base64.b64encode(os.urandom(32)).decode(),
    }
)
phi_globals_of(app_mod)["_key_provider"] = _TEST_PHI_PROVIDER
drafts.phi._key_provider = _TEST_PHI_PROVIDER

TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
PATIENT = 1737
OTHER_PATIENT = 1042
CLINICIAN_ID, PATIENT_USER_ID, OTHER_PATIENT_USER_ID = 900, 901, 902

V1_TEXT = "Version one, approved."
V2_TEXT = "Version two, still pending."


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    app_mod.AgentDraftProvenance.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add_all([
        app_mod.Patient(id=PATIENT, name="Demo Patient"),
        app_mod.Patient(id=OTHER_PATIENT, name="Other Patient"),
        app_mod.User(id=CLINICIAN_ID, username="drkim", role="clinician", is_active=True),
        app_mod.User(id=PATIENT_USER_ID, username="patient-1737", role="patient", is_active=True),
        app_mod.User(id=OTHER_PATIENT_USER_ID, username="patient-1042", role="patient",
                     is_active=True),
    ])
    db.flush()
    db.add_all([
        models.PatientAccessGrant(user_id=CLINICIAN_ID, patient_id=PATIENT),
        models.PatientAccessGrant(user_id=PATIENT_USER_ID, patient_id=PATIENT),
        models.PatientAccessGrant(user_id=OTHER_PATIENT_USER_ID, patient_id=OTHER_PATIENT),
    ])
    db.commit()

    monkeypatch.setattr(app_mod.settings, "internal_service_token", TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = lambda: db
    yield TestClient(app_mod.app), db
    app_mod.app.dependency_overrides.clear()
    db.close()


def _headers(user_id: int, name: str):
    return {"X-Internal-Token": TOKEN, "X-Actor-Id": str(user_id), "X-Actor-Name": name}


CLINICIAN = _headers(CLINICIAN_ID, "drkim")
PATIENT_H = _headers(PATIENT_USER_ID, "patient-1737")
OTHER_H = _headers(OTHER_PATIENT_USER_ID, "patient-1042")


_draft_correlation_ids = itertools.count(1)


def _draft(db, text, *, patient_id=PATIENT, passed=True):
    # Each call is its own "generation" — a distinct, server-generated
    # correlation_id every time (migration 036, review fix
    # ALC-CORR-COLLISION), never one hardcoded id reused across versions.
    row = drafts.create_draft(
        db, patient_id=patient_id, generated_text=text,
        correlation_id=f"corr-portal-{next(_draft_correlation_ids)}",
        provenance_label=drafts.LABEL_FIXTURE, model_id="scripted-model-v0",
        prompt_version="summary-agent-v1",
        citations=[{"source_id": "POL-001", "source_version": "2026-08-01",
                    "citation_id": "POL-001@2026-08-01", "category": "policy"}],
    )
    drafts.record_validation(db, row, passed=passed,
                             validation_code=None if passed else "REFUSED_NO_CLAIMS")
    db.commit()
    return row


def test_a_patient_cannot_read_another_patients_agent_summary(client):
    api, db = client
    approved = _draft(db, V1_TEXT)
    drafts.decide(db, approved, approve=True, reviewed_by=CLINICIAN_ID)
    db.commit()

    denied = api.get(f"/patients/{PATIENT}/agent-summary", headers=OTHER_H)
    assert denied.status_code == 403, "a grant for another chart is not a grant for this one"
    assert V1_TEXT not in denied.text

    # The clinician-only draft route is closed to the patient it belongs to too:
    # holding own_record.read is not holding summary_review.decide.
    assert api.get(f"/patients/{PATIENT}/agent-draft", headers=PATIENT_H).status_code == 403


def test_a_pending_or_rejected_draft_is_never_displayed(client):
    api, db = client
    pending = _draft(db, "Pending text.")

    hidden = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H)
    assert hidden.status_code == 200 and hidden.json()["available"] is False
    assert "Pending text." not in hidden.text
    # W9.1: the patient home's status chip needs to tell "waiting for a
    # clinician" apart from "nothing requested" without ever seeing the text
    # above — a validated-but-undecided draft is exactly the "pending" case.
    assert hidden.json()["status"] == "pending"

    drafts.decide(db, pending, approve=False, reviewed_by=CLINICIAN_ID)
    db.commit()
    after = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H)
    assert after.json()["available"] is False, "a rejection is not a weaker approval"
    assert "Pending text." not in after.text
    # Rejected is not "pending" either — there is nothing left waiting on a
    # clinician, so the home should offer to request again, not keep saying
    # "waiting for review" about a version that already got its decision.
    assert after.json()["status"] == "none"

    # The clinician may read exactly what the patient may not.
    clinician_view = api.get(f"/patients/{PATIENT}/agent-draft", headers=CLINICIAN)
    assert clinician_view.status_code == 200
    assert clinician_view.json()["generated_text"] == "Pending text."


def test_clinician_approval_exposes_the_exact_stored_text(client):
    api, db = client
    row = _draft(db, V1_TEXT)

    decided = api.post(f"/agent-drafts/{row.id}/decision", json={"decision": "approved"},
                       headers=CLINICIAN)
    assert decided.status_code == 200 and decided.json()["status"] == drafts.APPROVED

    shown = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()
    assert shown["available"] is True
    assert shown["status"] == "approved"
    assert shown["generated_text"] == V1_TEXT, "the exact stored text, never a regeneration"
    assert shown["version"] == 1
    assert shown["provenance_label"] == drafts.LABEL_FIXTURE
    assert [c["citation_id"] for c in shown["citations"]] == ["POL-001@2026-08-01"]


def test_a_pending_version_2_does_not_replace_an_approved_version_1(client):
    api, db = client
    v1 = _draft(db, V1_TEXT)
    api.post(f"/agent-drafts/{v1.id}/decision", json={"decision": "approved"}, headers=CLINICIAN)

    v2 = _draft(db, V2_TEXT)
    assert v2.version == 2

    still_v1 = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()
    assert still_v1["version"] == 1
    assert still_v1["generated_text"] == V1_TEXT
    assert V2_TEXT not in str(still_v1), "an unapproved regeneration never reaches the patient"

    api.post(f"/agent-drafts/{v2.id}/decision", json={"decision": "approved"}, headers=CLINICIAN)
    now_v2 = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()
    assert now_v2["version"] == 2 and now_v2["generated_text"] == V2_TEXT


def test_a_patient_can_request_their_own_summary_and_not_another_patients(client):
    """Asking is the demo's first move, and it must not become reading.

    This one runs the real generation path (no Bedrock configured, so it takes
    the deterministic fallback), because the point is what the RESPONSE carries
    back to the patient who asked.
    """
    api, db = client

    mine = api.post(f"/patients/{PATIENT}/agent-summary/request", headers=PATIENT_H)
    assert mine.status_code == 201
    body = mine.json()
    assert body["patient_id"] == PATIENT and body["version"] == 1
    assert body["correlation_id"], "the receipt names the trace this request started"
    assert "generated_text" not in body, "asking is not reading"
    assert "Riverbend releases laboratory results" not in mine.text, "no draft text, anywhere"

    # The draft it created is not displayable to them yet.
    assert api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()["available"] is False

    theirs = api.post(f"/patients/{OTHER_PATIENT}/agent-summary/request", headers=PATIENT_H)
    assert theirs.status_code == 403, "a patient holds a grant for exactly one chart"
    assert app_mod._latest_draft(db, OTHER_PATIENT) is None, "and nothing was generated for it"


# --- two clinicians, deliberately overlapping on one patient ---------------- #
#
# Mirrors the real seed's matrix (db/seed/generate_seed.py, 2026-08-22):
# drkim holds {1042, 1737, 1738}, drnguyen holds {1738, 1739} — 1738 is the
# deliberate overlap so a shared-queue case is demonstrable, and each is
# excluded from the other's exclusive patient. Applied here to the AGENT-DRAFT
# path specifically (generation/read/decision), which is a SEPARATE review
# surface from the deterministic record-review queue
# (services/records-service/review_queue.py) — both are gated the same way,
# through the same permission and the same patient_access_gate, but a draft
# approved/rejected here has no effect on a record decided there, and vice
# versa. Reuses the fixture's existing PATIENT/OTHER_PATIENT ids as the shared
# and exclusive patients respectively, so this does not need its own schema.

SECOND_CLINICIAN_ID = 903


@pytest.fixture
def two_clinician_client(client):
    api, db = client
    # A second clinician, granted the SAME chart (PATIENT) as CLINICIAN_ID —
    # the overlap — and nothing else.
    db.add(app_mod.User(id=SECOND_CLINICIAN_ID, username="drnguyen", role="clinician", is_active=True))
    db.flush()
    db.add(models.PatientAccessGrant(user_id=SECOND_CLINICIAN_ID, patient_id=PATIENT))
    db.commit()
    return api, db


def _headers_for(uid, uname):
    return {"X-Internal-Token": TOKEN, "X-Actor-Id": str(uid), "X-Actor-Name": uname}


SECOND_CLINICIAN = _headers_for(SECOND_CLINICIAN_ID, "drnguyen")


def test_two_clinicians_can_each_generate_and_read_a_shared_patients_draft(two_clinician_client):
    api, db = two_clinician_client

    generated = api.post(f"/patients/{PATIENT}/agent-draft", headers=CLINICIAN)
    assert generated.status_code == 201

    seen_by_first = api.get(f"/patients/{PATIENT}/agent-draft", headers=CLINICIAN)
    seen_by_second = api.get(f"/patients/{PATIENT}/agent-draft", headers=SECOND_CLINICIAN)
    assert seen_by_first.status_code == 200 and seen_by_second.status_code == 200
    assert seen_by_first.json()["generated_text"] == seen_by_second.json()["generated_text"]


def test_the_second_clinician_cannot_decide_a_draft_the_first_already_decided(two_clinician_client):
    """The agent-draft equivalent of the record-review queue's own
    cannot-overwrite guarantee: a shared grant means both can attempt a
    decision, not that either attempt is free to ignore the other's."""
    api, db = two_clinician_client
    draft = api.post(f"/patients/{PATIENT}/agent-draft", headers=CLINICIAN).json()

    first = api.post(f"/agent-drafts/{draft['id']}/decision", json={"decision": "approved"},
                     headers=CLINICIAN)
    assert first.status_code == 200

    second = api.post(f"/agent-drafts/{draft['id']}/decision", json={"decision": "rejected"},
                      headers=SECOND_CLINICIAN)
    assert second.status_code == 409, "an already-decided draft must not be overwritable by anyone"

    final = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H).json()
    assert final["available"] is True, "the first clinician's approval must stand"


def test_a_clinician_ungranted_for_a_patient_cannot_reach_its_draft_even_if_generated_by_another(two_clinician_client):
    """drnguyen is not granted OTHER_PATIENT — the exclusive-patient half of
    the overlap test. Generating a draft under one clinician's own grant must
    not become readable by a second clinician who was never granted it."""
    api, db = two_clinician_client
    api.post(f"/patients/{OTHER_PATIENT}/agent-draft", headers=CLINICIAN)

    denied = api.get(f"/patients/{OTHER_PATIENT}/agent-draft", headers=SECOND_CLINICIAN)
    assert denied.status_code == 403


# --- W10 Final Stage 4 review fixes -----------------------------------------


def test_the_same_x_request_id_never_becomes_two_drafts_shared_lifecycle_id(client):
    """ALC-CORR-COLLISION: a caller-supplied X-Request-Id must never become
    the draft's lifecycle correlation_id — it stays request/audit metadata
    only. Two generations sharing one X-Request-Id must still get two
    distinct, server-generated lifecycle ids, and each must reconstruct as
    its own independent event stream."""
    api, db = client
    same_request_id = {**CLINICIAN, "X-Request-Id": "shared-req-id-reused-by-a-caller"}

    # Two SEPARATE generations for the same patient (a regeneration), both
    # asserting the identical caller-supplied X-Request-Id — the collision
    # risk this finding is about has nothing to do with which patient.
    first = api.post(f"/patients/{PATIENT}/agent-draft", headers=same_request_id)
    second = api.post(f"/patients/{PATIENT}/agent-draft", headers=same_request_id)
    assert first.status_code == 201 and second.status_code == 201

    row1 = db.get(app_mod.AgentDraftProvenance, first.json()["id"])
    row2 = db.get(app_mod.AgentDraftProvenance, second.json()["id"])

    assert row1.correlation_id != row2.correlation_id
    assert row1.correlation_id != "shared-req-id-reused-by-a-caller"
    assert row2.correlation_id != "shared-req-id-reused-by-a-caller"

    trace1 = app_mod.agent_lifecycle.reconstruct(db, row1.correlation_id)
    trace2 = app_mod.agent_lifecycle.reconstruct(db, row2.correlation_id)
    assert trace1.events and trace2.events, "both generations must have persisted a real stream"

    # Independent: the total persisted row count is exactly the sum of the
    # two streams — neither leaked an event into the other's correlation_id.
    total_rows = db.query(models.AgentLifecycleEvent).filter(
        models.AgentLifecycleEvent.correlation_id.in_([row1.correlation_id, row2.correlation_id])
    ).count()
    assert total_rows == len(trace1.events) + len(trace2.events)


def test_refreshing_an_approved_summary_appends_exactly_one_display_event(client):
    """ALC-DISPLAY-REPEAT: two sequential approved-summary reads must both
    succeed and return the same version, but the durable stream may only
    ever record ONE display event for that draft's lifecycle.

    Seeds a real, full-shape generation trace directly (the actual route
    falls back — no Bedrock model is configured in this test environment —
    and a fallback trace is a genuinely shorter shape not held to
    is_acceptable(), per libs.agent_provenance's own module docstring) so
    the reconstructed lifecycle can be checked against the real grammar
    end to end, the same way a real generation's would be."""
    from libs.agent_provenance import ProvenanceLabel, TraceRecorder

    api, db = client
    correlation_id = "corr-display-repeat-1"
    trace = TraceRecorder(correlation_id)
    trace.request(actor_role="clinician")
    trace.provider_call(label=ProvenanceLabel.REAL, model_id="model-x", latency_ms=100)
    trace.agent_decision(tool_name="search_documents", turn=1, stop_reason="tool_use")
    trace.retrieval(document_count=1, citation_ids=["c1"], categories=["policy"])
    trace.provider_call(label=ProvenanceLabel.REAL, model_id="model-x", latency_ms=90)
    trace.agent_decision(tool_name=None, turn=2, stop_reason="end_turn")

    draft = drafts.create_draft(
        db, patient_id=PATIENT, generated_text=V1_TEXT, correlation_id=correlation_id,
        provenance_label=drafts.LABEL_REAL, model_id="model-x", prompt_version="v1",
        citations=[{"source_id": "POL-001", "source_version": "2026-08-01",
                    "citation_id": "c1", "category": "policy"}],
        trace=trace,
    )
    drafts.record_validation(db, draft, passed=True, trace=trace)
    app_mod.agent_lifecycle.persist(db, correlation_id, trace.events)
    db.commit()

    decided = api.post(f"/agent-drafts/{draft.id}/decision", json={"decision": "approved"}, headers=CLINICIAN)
    assert decided.status_code == 200

    first = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H)
    second = api.get(f"/patients/{PATIENT}/agent-summary", headers=PATIENT_H)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["version"] == second.json()["version"] == draft.version

    trace_out = app_mod.agent_lifecycle.reconstruct(db, correlation_id)
    display_events = [e for e in trace_out.events if e.stage.value == "display"]
    assert len(display_events) == 1, "exactly one display row, no matter how many times it was read"

    assert trace_out.is_complete()
    assert trace_out.is_ordered()
    assert trace_out.is_grounded()
    assert trace_out.is_acceptable()
