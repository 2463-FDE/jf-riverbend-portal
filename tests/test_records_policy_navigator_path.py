"""Tests for records-service's policy navigator orchestration
(services/records-service/policy_navigator_path.py) — specifically the
retrieval-infrastructure fallback path `run_policy_navigator` itself never
covers (a Postgres/embedding-provider construction failure), so this never
raises out of the route, and (review fix PN-CONN-LEAK) never opens a
Postgres connection it fails to close.

Review fix PN-FLUSH-ESCAPE: this module takes no `db` and persists no
usage accounting at all — that is entirely app.py's /policy/ask route's
job, after this function has already returned. See
tests/test_records_policy_route.py for that coverage.
"""
from conftest import load_module

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
