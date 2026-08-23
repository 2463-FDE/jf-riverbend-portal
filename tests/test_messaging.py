"""Secure patient-clinician messaging (W9.2), driven against real
records-service routes over a real SQLite session — the same shape as
tests/test_agent_portal_path.py, because the authorization under test here
is the identical mechanism: a role permission plus a per-(actor, patient)
grant, never a caller-supplied claim.
"""
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_messaging")
models = sys.modules[app_mod.Patient.__module__]

TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"
PATIENT = 1737
OTHER_PATIENT = 1042
CLINICIAN_ID, SECOND_CLINICIAN_ID = 900, 905
PATIENT_USER_ID, OTHER_PATIENT_USER_ID = 901, 902
UNGRANTED_CLINICIAN_ID = 906


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    app_mod.Patient.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add_all([
        app_mod.Patient(id=PATIENT, name="Priya Khan"),
        app_mod.Patient(id=OTHER_PATIENT, name="Maria Gonzalez"),
        app_mod.User(id=CLINICIAN_ID, username="drkim", full_name="Dr. Grace Kim",
                     role="clinician", is_active=True),
        app_mod.User(id=SECOND_CLINICIAN_ID, username="drnguyen", full_name="Dr. Anita Nguyen",
                     role="clinician", is_active=True),
        app_mod.User(id=UNGRANTED_CLINICIAN_ID, username="drpatel", full_name="Dr. Anil Patel",
                     role="clinician", is_active=True),
        app_mod.User(id=PATIENT_USER_ID, username="patient-1737", full_name="Priya Khan",
                     role="patient", patient_id=PATIENT, is_active=True),
        app_mod.User(id=OTHER_PATIENT_USER_ID, username="patient-1042", full_name="Maria Gonzalez",
                     role="patient", patient_id=OTHER_PATIENT, is_active=True),
    ])
    db.flush()
    db.add_all([
        models.PatientAccessGrant(user_id=CLINICIAN_ID, patient_id=PATIENT),
        models.PatientAccessGrant(user_id=SECOND_CLINICIAN_ID, patient_id=PATIENT),
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
SECOND_CLINICIAN = _headers(SECOND_CLINICIAN_ID, "drnguyen")
UNGRANTED_CLINICIAN = _headers(UNGRANTED_CLINICIAN_ID, "drpatel")
PATIENT_H = _headers(PATIENT_USER_ID, "patient-1737")
OTHER_PATIENT_H = _headers(OTHER_PATIENT_USER_ID, "patient-1042")


def _create(api, headers, patient_id=PATIENT, subject="Question about my results", body="Hi, quick question.",
            key="key-1"):
    return api.post(
        f"/patients/{patient_id}/threads",
        json={"subject": subject, "body": body, "idempotency_key": key},
        headers=headers,
    )


def test_a_patient_creates_and_reads_their_own_thread(client):
    api, db = client
    created = _create(api, PATIENT_H)
    assert created.status_code == 201
    thread_id = created.json()["id"]
    assert created.json()["messages"][0]["body"] == "Hi, quick question."
    assert created.json()["messages"][0]["sender_name"] == "Priya Khan"

    fetched = api.get(f"/threads/{thread_id}", headers=PATIENT_H)
    assert fetched.status_code == 200
    assert fetched.json()["subject"] == "Question about my results"
    assert len(fetched.json()["messages"]) == 1


def test_a_granted_clinician_cannot_originate_a_thread(client):
    """Round-1 review: permission + grant alone let a granted clinician call
    this route directly and start a thread — contrary to the client's UX
    (a clinician replies, closes, reopens; a patient starts). drkim holds
    both messages.write and an active grant for PATIENT, so only the
    role+own-chart check added for this finding stops it."""
    api, db = client
    resp = _create(api, CLINICIAN)
    assert resp.status_code == 403
    assert "quick question" not in resp.text


def test_a_patient_cannot_originate_a_thread_on_another_patients_chart(client):
    """A patient's own role check is not enough by itself — it must also be
    THEIR chart. OTHER_PATIENT_USER_ID is genuinely a patient, just not
    PATIENT's patient, and holds no grant for PATIENT either."""
    api, db = client
    resp = _create(api, OTHER_PATIENT_H, patient_id=PATIENT)
    assert resp.status_code == 403


def test_a_clinician_with_an_active_grant_reads_and_replies(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]

    inbox = api.get("/threads", headers=CLINICIAN)
    assert inbox.status_code == 200
    assert any(t["id"] == thread_id for t in inbox.json()["items"])

    reply = api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "Your result looks fine.", "idempotency_key": "clin-key-1"},
        headers=CLINICIAN,
    )
    assert reply.status_code == 201
    assert reply.json()["sender_name"] == "Dr. Grace Kim"

    thread = api.get(f"/threads/{thread_id}", headers=PATIENT_H).json()
    assert [m["body"] for m in thread["messages"]] == ["Hi, quick question.", "Your result looks fine."]


def test_cross_patient_attempts_fail_before_any_body_loads(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]

    # An ungranted clinician's read: 404, not 403 with a hint the thread
    # exists — an unauthorized id and a nonexistent one must look identical.
    denied_read = api.get(f"/threads/{thread_id}", headers=UNGRANTED_CLINICIAN)
    assert denied_read.status_code == 404
    assert "quick question" not in denied_read.text

    denied_reply = api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "I should not be able to send this.", "idempotency_key": "bad-1"},
        headers=UNGRANTED_CLINICIAN,
    )
    assert denied_reply.status_code == 404

    # A different patient entirely: same result, same reason hidden.
    other_patient_read = api.get(f"/threads/{thread_id}", headers=OTHER_PATIENT_H)
    assert other_patient_read.status_code == 404


def test_a_revoked_grant_closes_access_immediately(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]
    assert api.get(f"/threads/{thread_id}", headers=CLINICIAN).status_code == 200

    grant = db.execute(
        select(models.PatientAccessGrant).where(
            models.PatientAccessGrant.user_id == CLINICIAN_ID,
            models.PatientAccessGrant.patient_id == PATIENT,
        )
    ).scalar_one()
    from datetime import datetime, timezone
    grant.revoked_at = datetime.now(timezone.utc)
    db.commit()

    assert api.get(f"/threads/{thread_id}", headers=CLINICIAN).status_code == 404
    # The patient's own access is untouched — revoking staff access does not
    # touch the patient's grant for their own chart.
    assert api.get(f"/threads/{thread_id}", headers=PATIENT_H).status_code == 200


def test_a_duplicate_send_is_idempotent(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]

    first = api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "Sending this once.", "idempotency_key": "retry-key"},
        headers=CLINICIAN,
    )
    second = api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "Sending this once.", "idempotency_key": "retry-key"},
        headers=CLINICIAN,
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"], "a retried send must not create a second message"

    thread = api.get(f"/threads/{thread_id}", headers=PATIENT_H).json()
    assert len(thread["messages"]) == 2  # the original patient message + exactly one reply


def test_the_same_idempotency_key_reused_on_a_different_thread_is_not_confused_for_a_replay(client):
    """Round-1 review (MSG-002): the same sender reusing one idempotency key
    across two DIFFERENT threads must create a genuinely new message in the
    second thread, not silently return the first thread's message as if it
    had just been posted where the caller actually asked."""
    api, db = client
    thread_a = _create(api, PATIENT_H, key="key-a").json()["id"]
    thread_b = _create(api, PATIENT_H, key="key-b").json()["id"]

    reply_a = api.post(
        f"/threads/{thread_a}/messages",
        json={"body": "Reply in thread A.", "idempotency_key": "shared-key"},
        headers=CLINICIAN,
    )
    reply_b = api.post(
        f"/threads/{thread_b}/messages",
        json={"body": "Reply in thread B.", "idempotency_key": "shared-key"},
        headers=CLINICIAN,
    )

    assert reply_a.status_code == 201 and reply_b.status_code == 201
    assert reply_a.json()["id"] != reply_b.json()["id"], "each thread must get its own message"
    assert reply_b.json()["thread_id"] == thread_b
    assert reply_b.json()["body"] == "Reply in thread B."

    messages_b = api.get(f"/threads/{thread_b}", headers=PATIENT_H).json()["messages"]
    assert any(m["body"] == "Reply in thread B." for m in messages_b)
    assert not any(m["body"] == "Reply in thread A." for m in messages_b)


def test_a_sent_message_has_no_edit_or_delete_route(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]
    message_id = api.get(f"/threads/{thread_id}", headers=PATIENT_H).json()["messages"][0]["id"]

    # No route matches these paths at all — FastAPI 404s rather than 405s,
    # which is exactly the property under test: there is no edit or delete
    # capability to be denied, because none was ever wired up.
    assert api.put(f"/threads/{thread_id}/messages/{message_id}", json={"body": "edited"}).status_code == 404
    assert api.delete(f"/threads/{thread_id}/messages/{message_id}").status_code == 404


def test_unread_state_is_per_user_not_per_thread(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]

    # Both clinicians share the grant; only one has read it so far.
    api.get(f"/threads/{thread_id}", headers=CLINICIAN)

    inbox_kim = api.get("/threads", headers=CLINICIAN).json()["items"]
    inbox_nguyen = api.get("/threads", headers=SECOND_CLINICIAN).json()["items"]
    kim_row = next(t for t in inbox_kim if t["id"] == thread_id)
    nguyen_row = next(t for t in inbox_nguyen if t["id"] == thread_id)

    assert kim_row["unread_count"] == 0, "drkim just read it"
    assert nguyen_row["unread_count"] == 1, "drnguyen has not opened it yet"


def test_two_clinicians_share_one_thread_without_overwriting_sender_identity(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]

    api.post(f"/threads/{thread_id}/messages",
             json={"body": "From Kim.", "idempotency_key": "kim-1"}, headers=CLINICIAN)
    api.post(f"/threads/{thread_id}/messages",
             json={"body": "From Nguyen.", "idempotency_key": "nguyen-1"}, headers=SECOND_CLINICIAN)

    messages = api.get(f"/threads/{thread_id}", headers=PATIENT_H).json()["messages"]
    by_body = {m["body"]: m for m in messages}
    assert by_body["From Kim."]["sender_name"] == "Dr. Grace Kim"
    assert by_body["From Kim."]["sender_user_id"] == CLINICIAN_ID
    assert by_body["From Nguyen."]["sender_name"] == "Dr. Anita Nguyen"
    assert by_body["From Nguyen."]["sender_user_id"] == SECOND_CLINICIAN_ID


def test_closing_and_reopening_is_staff_only_and_blocks_replies_while_closed(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]

    patient_close = api.post(f"/threads/{thread_id}/status", json={"status": "closed"}, headers=PATIENT_H)
    assert patient_close.status_code == 403, "a patient does not control the thread lifecycle"

    closed = api.post(f"/threads/{thread_id}/status", json={"status": "closed"}, headers=CLINICIAN)
    assert closed.status_code == 200 and closed.json()["status"] == "closed"

    blocked_reply = api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "Can I still reply?", "idempotency_key": "after-close"},
        headers=PATIENT_H,
    )
    assert blocked_reply.status_code == 409

    reopened = api.post(f"/threads/{thread_id}/status", json={"status": "open"}, headers=CLINICIAN)
    assert reopened.status_code == 200 and reopened.json()["status"] == "open"

    allowed_reply = api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "Now I can.", "idempotency_key": "after-reopen"},
        headers=PATIENT_H,
    )
    assert allowed_reply.status_code == 201


def test_blank_and_oversized_bodies_are_rejected(client):
    api, db = client
    thread_id = _create(api, PATIENT_H).json()["id"]

    blank = api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "   ", "idempotency_key": "blank-1"},
        headers=CLINICIAN,
    )
    assert blank.status_code == 400

    oversized = api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "x" * 4001, "idempotency_key": "big-1"},
        headers=CLINICIAN,
    )
    assert oversized.status_code == 400


def test_audit_rows_never_contain_the_message_body(client):
    api, db = client
    secret_body = "SENTINEL-DO-NOT-LOG-THIS-BODY"
    thread_id = _create(api, PATIENT_H, body=secret_body).json()["id"]
    api.post(
        f"/threads/{thread_id}/messages",
        json={"body": "another sentinel body text", "idempotency_key": "audit-check"},
        headers=CLINICIAN,
    )

    rows = db.execute(select(models.AuditLog.message)).scalars().all()
    # Round-1 review: create/reply used to commit their state, then call
    # _write_audit in a SEPARATE commit — an audit failure could 503 after
    # the thread/message was already durable, with no record it happened.
    # Fixed by adding the audit row to the SAME commit as the state change
    # (see create_thread/reply_to_thread's own comments); asserting the
    # rows actually exist here, not only that they are body-free, is what
    # would have caught the old two-commit version silently losing them.
    assert any("messages_thread_create" in r and f"thread_id={thread_id}" in r for r in rows if r)
    assert any("messages_reply" in r and f"thread_id={thread_id}" in r for r in rows if r)
    joined = "\n".join(r for r in rows if r)
    assert secret_body not in joined
    assert "another sentinel body text" not in joined
