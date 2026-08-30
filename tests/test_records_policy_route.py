"""Tests for records-service's POST /policy/ask
(services/records-service/app.py::ask_policy_navigator) — internal-token
gating, blank/oversize question rejection, and that the route derives role
from the DB (never trusts a header) before delegating to
policy_navigator_path.ask_policy_navigator. The navigator's own behavior is
covered by tests/test_policy_navigator_runtime.py; this file is about the
HTTP boundary only.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_module
from libs.policy_navigator import CitedSource, PolicyNavigatorResult

app_mod = load_module("services/records-service/app.py", "records_app_policy_route")

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod, "settings", app_mod.settings)
    app_mod.settings.internal_service_token = TEST_TOKEN
    monkeypatch.setattr(app_mod, "get_db", lambda: iter([None]))
    app_mod.app.dependency_overrides[app_mod.get_db] = lambda: iter([None])
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def _headers(actor_id="7"):
    return {"X-Internal-Token": TEST_TOKEN, "X-Actor-Id": actor_id}


def test_rejects_without_a_valid_internal_token(client):
    resp = client.post("/policy/ask", json={"question": "How does intake work?"}, headers={"X-Actor-Id": "7"})

    assert resp.status_code == 401


def test_rejects_a_blank_question(client, monkeypatch):
    monkeypatch.setattr(app_mod, "_actor_role", lambda db, actor_id: "clinician")

    resp = client.post("/policy/ask", json={"question": "   "}, headers=_headers())

    assert resp.status_code == 422


def test_rejects_an_oversize_question(client, monkeypatch):
    monkeypatch.setattr(app_mod, "_actor_role", lambda db, actor_id: "clinician")

    resp = client.post("/policy/ask", json={"question": "x" * 501}, headers=_headers())

    assert resp.status_code == 422


def test_derives_role_from_the_database_and_delegates_to_the_navigator(client, monkeypatch):
    monkeypatch.setattr(app_mod, "_actor_role", lambda db, actor_id: "roi_clerk")
    captured = {}

    def fake_ask(question, *, actor_role, model=None, db=None):
        captured["question"] = question
        captured["actor_role"] = actor_role
        return PolicyNavigatorResult(
            answer="Disclosures require minimum-necessary review [ROI-DISC-001@1.0#s].",
            citations=(
                CitedSource(
                    citation_id="ROI-DISC-001@1.0#s", source_id="ROI-DISC-001", source_version="1.0",
                    title="ROI and Disclosure Policy", section_id="s",
                ),
            ),
            label="fixture", model_id=None, termination_reason="answered",
        )

    monkeypatch.setattr(app_mod.policy_navigator_path, "ask_policy_navigator", fake_ask)

    resp = client.post("/policy/ask", json={"question": "What governs disclosures?"}, headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert captured["actor_role"] == "roi_clerk"
    assert captured["question"] == "What governs disclosures?"
    assert body["termination_reason"] == "answered"
    assert body["citations"][0]["source_id"] == "ROI-DISC-001"


def test_an_unresolvable_actor_still_gets_a_role_derived_response_not_an_error(client, monkeypatch):
    # _actor_role itself already returns "unknown" for a missing/invalid
    # actor_id rather than raising — this proves the route doesn't add its
    # own hard rejection on top, since scope_for_role("unknown") already
    # fails closed to an empty scope.
    monkeypatch.setattr(app_mod, "_actor_role", lambda db, actor_id: "unknown")
    monkeypatch.setattr(
        app_mod.policy_navigator_path, "ask_policy_navigator",
        lambda question, *, actor_role, model=None, db=None: PolicyNavigatorResult(
            answer="No approved policy evidence was found.", citations=(), label="real",
            model_id="m", termination_reason="no_evidence",
        ),
    )

    resp = client.post("/policy/ask", json={"question": "anything"}, headers={"X-Internal-Token": TEST_TOKEN})

    assert resp.status_code == 200
    assert resp.json()["termination_reason"] == "no_evidence"
