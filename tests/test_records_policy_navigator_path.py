"""Tests for records-service's policy navigator orchestration
(services/records-service/policy_navigator_path.py) — specifically the
retrieval-infrastructure fallback path `run_policy_navigator` itself never
covers (a Postgres/embedding-provider construction failure), so this never
raises out of the route.
"""
from conftest import load_module

path_mod = load_module("services/records-service/policy_navigator_path.py", "records_policy_navigator_path")


def test_an_unreachable_postgres_degrades_to_a_safe_fallback_never_raises(monkeypatch):
    def raise_connection_error():
        raise ConnectionError("could not connect to postgres")

    monkeypatch.setattr(path_mod, "_policy_connection", raise_connection_error)
    monkeypatch.setenv("POLICY_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")

    result = path_mod.ask_policy_navigator("a question", actor_role="clinician")

    assert result.termination_reason == "provider_error"
    assert result.label == "fallback"


def test_an_unconfigured_embedding_model_degrades_to_a_safe_fallback_never_raises(monkeypatch):
    monkeypatch.setattr(path_mod, "_policy_connection", lambda: None)
    monkeypatch.delenv("POLICY_EMBEDDING_MODEL_ID", raising=False)

    result = path_mod.ask_policy_navigator("a question", actor_role="clinician")

    assert result.termination_reason == "provider_error"
    assert result.label == "fallback"
