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
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module
from libs.policy_navigator import CitedSource, PolicyNavigatorResult
from libs.policy_navigator.contracts import UsageTurn

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

    def fake_ask(question, *, actor_role, model=None):
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
        lambda question, *, actor_role, model=None: PolicyNavigatorResult(
            answer="No approved policy evidence was found.", citations=(), label="real",
            model_id="m", termination_reason="no_evidence",
        ),
    )

    resp = client.post("/policy/ask", json={"question": "anything"}, headers={"X-Internal-Token": TEST_TOKEN})

    assert resp.status_code == 200
    assert resp.json()["termination_reason"] == "no_evidence"


# --- W10 Final Stage 5 sub-slice 3 / review fix PN-FLUSH-ESCAPE ------------
# Usage accounting is a SEPARATE step, after ask_policy_navigator has
# already returned — proven here at the route, since that is where the
# step now lives (policy_navigator_path.py itself takes no `db` at all).


@pytest.fixture
def client_with_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    app_mod.bedrock_usage.BedrockUsageEvent.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    monkeypatch.setattr(app_mod, "settings", app_mod.settings)
    app_mod.settings.internal_service_token = TEST_TOKEN
    app_mod.app.dependency_overrides[app_mod.get_db] = lambda: db
    monkeypatch.setattr(app_mod, "_actor_role", lambda db, actor_id: "clinician")
    yield TestClient(app_mod.app), db
    app_mod.app.dependency_overrides.clear()
    db.close()


def _fake_ask_with_usage(*, calls=None):
    def fake_ask(question, *, actor_role, model=None):
        if calls is not None:
            calls.append(1)
        return PolicyNavigatorResult(
            answer="Coverage stays active [SRC-001@1.0#overview].",
            citations=(), label="real", model_id="model-x", termination_reason="answered",
            usage=(UsageTurn(model_id="model-x", turn=1, input_tokens=80, output_tokens=15),),
        )
    return fake_ask


def test_successful_policy_usage_is_stored_exactly_once(client_with_db, monkeypatch):
    client, db = client_with_db
    monkeypatch.setattr(app_mod.policy_navigator_path, "ask_policy_navigator", _fake_ask_with_usage())

    resp = client.post("/policy/ask", json={"question": "How long does coverage last?"}, headers=_headers())

    assert resp.status_code == 200
    rows = app_mod.bedrock_usage.usage_for(db, use_case="policy_navigator_chat")
    assert len(rows) == 1
    assert rows[0].input_tokens == 80 and rows[0].output_tokens == 15


def test_the_navigator_executes_exactly_once_even_when_accounting_fails(client_with_db, monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod.policy_navigator_path, "ask_policy_navigator", _fake_ask_with_usage(calls=calls))
    monkeypatch.setattr(
        app_mod.bedrock_usage, "persist",
        lambda *a, **k: (_ for _ in ()).throw(SQLAlchemyError("boom")),
    )
    client, db = client_with_db

    resp = client.post("/policy/ask", json={"question": "How long does coverage last?"}, headers=_headers())

    assert resp.status_code == 200
    assert len(calls) == 1, "the navigator must never be rerun after an accounting failure"


def test_a_policy_accounting_failure_rolls_back_but_still_returns_the_original_answer(client_with_db, monkeypatch):
    monkeypatch.setattr(app_mod.policy_navigator_path, "ask_policy_navigator", _fake_ask_with_usage())
    monkeypatch.setattr(
        app_mod.bedrock_usage, "persist",
        lambda *a, **k: (_ for _ in ()).throw(SQLAlchemyError("boom")),
    )
    client, db = client_with_db

    resp = client.post("/policy/ask", json={"question": "How long does coverage last?"}, headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Coverage stays active [SRC-001@1.0#overview]."
    assert body["termination_reason"] == "answered"
    # The failed accounting attempt must not leave a half-written row behind.
    assert app_mod.bedrock_usage.usage_for(db, use_case="policy_navigator_chat") == []
