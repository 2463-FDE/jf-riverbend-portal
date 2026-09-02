"""Tests for db/policy_corpus_evaluate.py's connection/provider lifecycle
(review fix EVAL-VERIFY-BEDROCK-REGION): a --verify-only run is a
manifest/database parity check and must not require AWS_REGION or construct
the Bedrock embedding provider at all.
"""
import pytest

from conftest import load_module

evaluate_mod = load_module("db/policy_corpus_evaluate.py", "policy_corpus_evaluate_script")


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeFreshnessReport:
    def __init__(self, is_fresh):
        self.is_fresh = is_fresh

    def as_dict(self):
        return {"fresh": self.is_fresh}


@pytest.fixture(autouse=True)
def _valid_model_id(monkeypatch):
    monkeypatch.setenv("POLICY_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")


def _no_provider_construction(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("BedrockPolicyEmbeddingProvider must not be constructed for --verify-only")

    monkeypatch.setattr(evaluate_mod, "BedrockPolicyEmbeddingProvider", _fail)
    monkeypatch.setattr(evaluate_mod, "EmbeddingClient", _fail)


def test_verify_only_succeeds_with_aws_region_unset(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    _no_provider_construction(monkeypatch)
    fake_conn = _FakeConnection()
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: fake_conn)
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: _FakeFreshnessReport(True))

    exit_code = evaluate_mod.main(["--verify-only"])

    assert exit_code == 0
    assert fake_conn.closed is True


def test_not_fresh_also_never_constructs_the_provider(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    _no_provider_construction(monkeypatch)
    fake_conn = _FakeConnection()
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: fake_conn)
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: _FakeFreshnessReport(False))

    exit_code = evaluate_mod.main([])

    assert exit_code == 2
    assert fake_conn.closed is True


# --- W10 Metrics Stage 5: --publish ----------------------------------------


def test_publish_flag_off_by_default_never_pushes_anything(monkeypatch):
    _no_provider_construction(monkeypatch)
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: _FakeFreshnessReport(False))
    calls = []
    monkeypatch.setattr(evaluate_mod, "push_metrics", lambda **kw: calls.append(kw) or True)

    evaluate_mod.main([])

    assert calls == []


def test_publish_flag_pushes_freshness_only_metrics_for_a_stale_corpus(monkeypatch):
    """A stale corpus never runs the full evaluation, but its freshness
    numbers must still be publishable on their own — see
    policy_corpus_gauges' `report=None` case."""
    _no_provider_construction(monkeypatch)
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    freshness = _FakeFreshnessReport(False)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: freshness)
    gauge_calls = []
    monkeypatch.setattr(evaluate_mod, "policy_corpus_gauges", lambda **kw: gauge_calls.append(kw) or {"x": 1.0})
    push_calls = []
    monkeypatch.setattr(evaluate_mod, "push_metrics", lambda **kw: push_calls.append(kw) or True)
    monkeypatch.setenv("RAG_EVAL_PUSHGATEWAY_URL", "http://pushgateway:9091")

    exit_code = evaluate_mod.main(["--publish"])

    assert exit_code == 2
    assert gauge_calls == [{"freshness": freshness}]
    assert push_calls == [{"pushgateway_url": "http://pushgateway:9091", "corpus": "policy_corpus", "gauges": {"x": 1.0}}]


def test_a_failed_push_never_changes_the_scripts_own_exit_code(monkeypatch):
    _no_provider_construction(monkeypatch)
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: _FakeFreshnessReport(True))
    monkeypatch.setattr(evaluate_mod, "policy_corpus_gauges", lambda **kw: {})
    monkeypatch.setattr(evaluate_mod, "push_metrics", lambda **kw: False)

    exit_code = evaluate_mod.main(["--verify-only", "--publish"])

    assert exit_code == 0  # unchanged from the non-publishing verify-only case
