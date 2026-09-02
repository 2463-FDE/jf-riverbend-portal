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


def test_publish_flag_pushes_only_freshness_kind_for_a_stale_corpus(monkeypatch):
    """A stale corpus never runs the full evaluation — only the freshness
    snapshot is published, never kind="evaluation", so a stale/--verify-only
    run can never advance the completed-evaluation age (the review finding
    this regression-tests: freshness and evaluation are separate snapshots,
    never conflated)."""
    _no_provider_construction(monkeypatch)
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    freshness = _FakeFreshnessReport(False)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: freshness)
    gauge_calls = []
    monkeypatch.setattr(
        evaluate_mod, "policy_corpus_freshness_gauges", lambda **kw: gauge_calls.append(kw) or {"x": 1.0},
    )
    evaluation_gauge_calls = []
    monkeypatch.setattr(
        evaluate_mod, "policy_corpus_evaluation_gauges", lambda **kw: evaluation_gauge_calls.append(kw) or {"y": 1.0},
    )
    push_calls = []
    monkeypatch.setattr(evaluate_mod, "push_metrics", lambda **kw: push_calls.append(kw) or True)
    monkeypatch.setenv("RAG_EVAL_PUSHGATEWAY_URL", "http://pushgateway:9091")

    exit_code = evaluate_mod.main(["--publish"])

    assert exit_code == 2
    assert gauge_calls == [{"freshness": freshness}]
    assert evaluation_gauge_calls == []  # never built — no evaluation ran
    assert push_calls == [
        {"pushgateway_url": "http://pushgateway:9091", "corpus": "policy_corpus", "kind": "freshness", "gauges": {"x": 1.0}},
    ]


def test_verify_only_publish_also_pushes_only_freshness_kind(monkeypatch):
    """--verify-only is the other "no evaluation ran" path — same guarantee
    as the stale-corpus case above: evaluation age must not move."""
    _no_provider_construction(monkeypatch)
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: _FakeFreshnessReport(True))
    monkeypatch.setattr(evaluate_mod, "policy_corpus_freshness_gauges", lambda **kw: {"x": 1.0})
    evaluation_gauge_calls = []
    monkeypatch.setattr(
        evaluate_mod, "policy_corpus_evaluation_gauges", lambda **kw: evaluation_gauge_calls.append(kw) or {},
    )
    push_calls = []
    monkeypatch.setattr(evaluate_mod, "push_metrics", lambda **kw: push_calls.append(kw) or True)
    monkeypatch.setenv("RAG_EVAL_PUSHGATEWAY_URL", "http://pushgateway:9091")

    exit_code = evaluate_mod.main(["--verify-only", "--publish"])

    assert exit_code == 0
    assert evaluation_gauge_calls == []
    assert [call["kind"] for call in push_calls] == ["freshness"]


def test_a_failed_push_never_changes_the_scripts_own_exit_code(monkeypatch):
    _no_provider_construction(monkeypatch)
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: _FakeFreshnessReport(True))
    monkeypatch.setattr(evaluate_mod, "policy_corpus_freshness_gauges", lambda **kw: {})
    monkeypatch.setattr(evaluate_mod, "push_metrics", lambda **kw: False)

    exit_code = evaluate_mod.main(["--verify-only", "--publish"])

    assert exit_code == 0  # unchanged from the non-publishing verify-only case


def test_a_full_successful_evaluation_publishes_both_freshness_and_evaluation_kinds(monkeypatch):
    """The one path that DOES complete an evaluation — both snapshots must
    publish, each under its own kind."""
    monkeypatch.setattr(evaluate_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(evaluate_mod, "register_vector", lambda conn: None)
    freshness = _FakeFreshnessReport(True)
    monkeypatch.setattr(evaluate_mod, "check_corpus_freshness", lambda *a, **kw: freshness)
    monkeypatch.setattr(evaluate_mod, "EmbeddingClient", lambda **kw: object())
    monkeypatch.setattr(evaluate_mod, "BedrockPolicyEmbeddingProvider", lambda **kw: object())
    monkeypatch.setattr(evaluate_mod, "PolicyRetriever", lambda *a, **kw: object())
    monkeypatch.setattr(evaluate_mod, "KeywordPolicyRetriever", lambda *a, **kw: object())
    monkeypatch.setattr(evaluate_mod, "load_evaluation_cases", lambda path: [])
    monkeypatch.setattr(evaluate_mod, "load_aliases", lambda path: {})
    monkeypatch.setattr(evaluate_mod, "load_case_overrides", lambda path: {})
    monkeypatch.setattr(evaluate_mod, "load_manifest", lambda path: object())

    class _FakeEvalReport:
        unauthorized_retrieval_count = 0
        forbidden_citation_count = 0

        def as_dict(self):
            return {}

    monkeypatch.setattr(evaluate_mod, "evaluate_retrieval", lambda *a, **kw: _FakeEvalReport())
    monkeypatch.setattr(evaluate_mod, "policy_corpus_freshness_gauges", lambda **kw: {"f": 1.0})
    monkeypatch.setattr(evaluate_mod, "policy_corpus_evaluation_gauges", lambda **kw: {"e": 1.0})
    push_calls = []
    monkeypatch.setattr(evaluate_mod, "push_metrics", lambda **kw: push_calls.append(kw) or True)
    monkeypatch.setenv("RAG_EVAL_PUSHGATEWAY_URL", "http://pushgateway:9091")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    exit_code = evaluate_mod.main(["--publish"])

    assert exit_code == 0
    assert [call["kind"] for call in push_calls] == ["freshness", "evaluation"]
    assert push_calls[0]["gauges"] == {"f": 1.0}
    assert push_calls[1]["gauges"] == {"e": 1.0}
