"""Tests for records-service's policy navigator orchestration
(services/records-service/policy_navigator_path.py) — specifically the
retrieval-infrastructure fallback path `run_policy_navigator` itself never
covers (a Postgres/embedding-provider construction failure), so this never
raises out of the route, and (review fix PN-CONN-LEAK) never opens a
Postgres connection it fails to close.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module
from libs.policy_navigator import PolicyNavigatorResult
from libs.policy_navigator.contracts import UsageTurn

path_mod = load_module("services/records-service/policy_navigator_path.py", "records_policy_navigator_path")


def test_an_unreachable_postgres_degrades_to_a_safe_fallback_never_raises(monkeypatch):
    calls = []

    def raise_connection_error():
        calls.append(1)
        raise ConnectionError("could not connect to postgres")

    monkeypatch.setattr(path_mod, "_policy_connection", raise_connection_error)
    monkeypatch.setenv("POLICY_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    result = path_mod.ask_policy_navigator("a question", actor_role="clinician")

    assert result.termination_reason == "provider_error"
    assert result.label == "fallback"
    assert calls == [1]  # the connection path was genuinely reached, not skipped


def test_an_unconfigured_embedding_model_degrades_to_a_safe_fallback_never_raises(monkeypatch):
    monkeypatch.setattr(path_mod, "_policy_connection", lambda: None)
    monkeypatch.delenv("POLICY_EMBEDDING_MODEL_ID", raising=False)

    result = path_mod.ask_policy_navigator("a question", actor_role="clinician")

    assert result.termination_reason == "provider_error"
    assert result.label == "fallback"


def test_an_unconfigured_embedding_model_never_opens_a_postgres_connection(monkeypatch):
    # Review fix PN-CONN-LEAK: the old code opened the connection FIRST, so
    # an embedding-provider failure left it open with nothing to close it.
    # The provider must now be validated before any connection is attempted.
    calls = []
    monkeypatch.setattr(path_mod, "_policy_connection", lambda: calls.append(1))
    monkeypatch.delenv("POLICY_EMBEDDING_MODEL_ID", raising=False)

    path_mod.ask_policy_navigator("a question", actor_role="clinician")

    assert calls == []


# --- W10 Final Stage 5 sub-slice 3: durable usage accounting ---------------


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    path_mod.bedrock_usage.BedrockUsageEvent.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_a_db_session_persists_the_navigators_own_reported_usage(db, monkeypatch):
    monkeypatch.setattr(path_mod, "_policy_connection", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setenv("POLICY_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(
        path_mod, "run_policy_navigator",
        lambda question, *, scope, retriever, model=None, label=None: PolicyNavigatorResult(
            answer="Cited answer.", citations=(), label="real", model_id="model-x",
            termination_reason="answered", usage=(UsageTurn(model_id="model-x", turn=1,
                                                            input_tokens=80, output_tokens=15),),
        ),
    )

    path_mod.ask_policy_navigator("a question", actor_role="clinician", db=db)
    db.commit()

    rows = path_mod.bedrock_usage.usage_for(db, use_case="policy_navigator_chat")
    assert len(rows) == 1
    assert rows[0].input_tokens == 80 and rows[0].output_tokens == 15


def test_no_db_session_skips_usage_persistence_entirely(monkeypatch):
    # db=None (every existing test's call shape) must never attempt a write.
    monkeypatch.setattr(path_mod, "_policy_connection", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setenv("POLICY_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(
        path_mod, "run_policy_navigator",
        lambda question, *, scope, retriever, model=None, label=None: PolicyNavigatorResult(
            answer="Cited answer.", citations=(), label="real", model_id="model-x",
            termination_reason="answered", usage=(UsageTurn(model_id="model-x", turn=1,
                                                            input_tokens=80, output_tokens=15),),
        ),
    )
    calls = []
    monkeypatch.setattr(path_mod.bedrock_usage, "persist", lambda *a, **k: calls.append((a, k)))

    path_mod.ask_policy_navigator("a question", actor_role="clinician")

    assert calls == []
