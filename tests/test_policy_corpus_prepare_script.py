"""Tests for db/policy_corpus_prepare.py — W10 Final 2 Stage 2's single
idempotent operator command (run inside records-service's own container via
`make rag-prepare`, never on an undocumented host Python environment).

Mirrors tests/test_policy_corpus_ingest_script.py's mocked-connection
approach: no real Postgres or Bedrock call here (those already have their
own coverage — libs/policy_corpus's own unit/integration tests, and this
file's sibling tests/integration/test_policy_corpus_prepare_script.py for a
real-Postgres/fake-embedding-provider proof). This file is about the
wrapper's OWN new behavior: configuration validated before any connection,
an already-fresh corpus never touching the embedding provider at all, and a
stale corpus re-checking freshness after ingesting.
"""
import pytest

from conftest import load_module

prepare_mod = load_module("db/policy_corpus_prepare.py", "policy_corpus_prepare_script")

_VALID_ENV = {
    "POLICY_EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
    "AWS_REGION": "us-east-1",
    "DB_HOST": "localhost",
    "DB_NAME": "riverbend",
    "DB_USER": "riverbend_app",
    "DB_PASSWORD": "test-password",
}


def _set_valid_env(monkeypatch):
    for key, value in _VALID_ENV.items():
        monkeypatch.setenv(key, value)


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeFreshness:
    def __init__(self, is_fresh, corpus_id="riverbend-policy-v1", database_documents=16, embedded_chunks=200):
        self.is_fresh = is_fresh
        self.corpus_id = corpus_id
        self.database_documents = database_documents
        self.embedded_chunks = embedded_chunks


class _FakeReport:
    documents_upserted = 1
    documents_deactivated = 0
    chunks_written = 3
    chunks_skipped = 0
    embeddings_written = 3
    embeddings_skipped = 0


# --- configuration validated before any connection --------------------------


@pytest.mark.parametrize("missing_var", list(_VALID_ENV))
def test_a_missing_required_variable_fails_before_any_connection(monkeypatch, missing_var):
    _set_valid_env(monkeypatch)
    monkeypatch.delenv(missing_var, raising=False)
    calls = []
    monkeypatch.setattr(prepare_mod.psycopg2, "connect", lambda **kw: calls.append(1))

    with pytest.raises(SystemExit, match=missing_var):
        prepare_mod.main()

    assert calls == []


@pytest.mark.parametrize("placeholder", ["", "changeme"])
def test_a_placeholder_value_is_treated_the_same_as_missing(monkeypatch, placeholder):
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("POLICY_EMBEDDING_MODEL_ID", placeholder)
    calls = []
    monkeypatch.setattr(prepare_mod.psycopg2, "connect", lambda **kw: calls.append(1))

    with pytest.raises(SystemExit, match="POLICY_EMBEDDING_MODEL_ID"):
        prepare_mod.main()

    assert calls == []


def test_the_error_message_never_contains_a_configured_value(monkeypatch, capsys):
    """Presence/non-placeholder validation only — never printed or logged."""
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("DB_PASSWORD", "")
    monkeypatch.setattr(prepare_mod.psycopg2, "connect", lambda **kw: (_ for _ in ()).throw(AssertionError))

    with pytest.raises(SystemExit) as excinfo:
        prepare_mod.main()

    assert _VALID_ENV["POLICY_EMBEDDING_MODEL_ID"] not in str(excinfo.value)
    assert "DB_PASSWORD" in str(excinfo.value)


# --- already-fresh corpus never touches the embedding provider --------------


def test_an_already_fresh_corpus_never_constructs_the_embedding_provider(monkeypatch):
    _set_valid_env(monkeypatch)
    fake_conn = _FakeConnection()
    monkeypatch.setattr(prepare_mod.psycopg2, "connect", lambda **kw: fake_conn)
    monkeypatch.setattr(prepare_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(prepare_mod, "check_corpus_freshness", lambda *a, **k: _FakeFreshness(is_fresh=True))

    def _fail_if_constructed(*a, **k):
        raise AssertionError("BedrockPolicyEmbeddingProvider must not be constructed when already fresh")

    monkeypatch.setattr(prepare_mod, "BedrockPolicyEmbeddingProvider", _fail_if_constructed)
    ingest_calls = []
    monkeypatch.setattr(prepare_mod, "ingest_corpus", lambda *a, **k: ingest_calls.append(1))

    result = prepare_mod.main()

    assert result == 0
    assert ingest_calls == []
    assert fake_conn.closed is True


def test_an_already_fresh_corpus_reports_a_categorical_skipped_status(monkeypatch, capsys):
    _set_valid_env(monkeypatch)
    monkeypatch.setattr(prepare_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(prepare_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(
        prepare_mod, "check_corpus_freshness",
        lambda *a, **k: _FakeFreshness(is_fresh=True, corpus_id="riverbend-policy-v1", database_documents=16, embedded_chunks=200),
    )

    prepare_mod.main()

    out = capsys.readouterr().out
    assert "status=fresh" in out
    assert "action=skipped" in out
    assert "corpus_id=riverbend-policy-v1" in out
    assert "documents=16" in out
    assert "chunks=200" in out


# --- stale/missing corpus ingests, then re-checks freshness -----------------


def test_a_stale_corpus_ingests_and_reports_ready_when_freshness_agrees_afterward(monkeypatch, capsys):
    _set_valid_env(monkeypatch)
    monkeypatch.setattr(prepare_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(prepare_mod, "register_vector", lambda conn: None)

    freshness_results = iter([_FakeFreshness(is_fresh=False), _FakeFreshness(is_fresh=True)])
    monkeypatch.setattr(prepare_mod, "check_corpus_freshness", lambda *a, **k: next(freshness_results))
    monkeypatch.setattr(prepare_mod, "BedrockPolicyEmbeddingProvider", lambda **k: object())
    monkeypatch.setattr(prepare_mod, "EmbeddingClient", lambda **k: object())
    ingest_calls = []
    monkeypatch.setattr(prepare_mod, "ingest_corpus", lambda *a, **k: (ingest_calls.append(1), _FakeReport())[1])

    result = prepare_mod.main()

    assert result == 0
    assert ingest_calls == [1]
    out = capsys.readouterr().out
    assert "status=ready" in out
    assert "action=ingested" in out


def test_a_still_stale_corpus_after_ingestion_returns_a_nonzero_exit(monkeypatch, capsys):
    """Deliberately truthful: if the manifest and database still disagree
    after ingest_corpus runs (e.g. a partial provider failure it swallowed
    internally), this must be reported as still_stale, not a false ready."""
    _set_valid_env(monkeypatch)
    monkeypatch.setattr(prepare_mod.psycopg2, "connect", lambda **kw: _FakeConnection())
    monkeypatch.setattr(prepare_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(prepare_mod, "check_corpus_freshness", lambda *a, **k: _FakeFreshness(is_fresh=False))
    monkeypatch.setattr(prepare_mod, "BedrockPolicyEmbeddingProvider", lambda **k: object())
    monkeypatch.setattr(prepare_mod, "EmbeddingClient", lambda **k: object())
    monkeypatch.setattr(prepare_mod, "ingest_corpus", lambda *a, **k: _FakeReport())

    result = prepare_mod.main()

    assert result == 2
    assert "status=still_stale" in capsys.readouterr().out


def test_an_ingestion_failure_still_closes_the_connection(monkeypatch):
    _set_valid_env(monkeypatch)
    fake_conn = _FakeConnection()
    monkeypatch.setattr(prepare_mod.psycopg2, "connect", lambda **kw: fake_conn)
    monkeypatch.setattr(prepare_mod, "register_vector", lambda conn: None)
    monkeypatch.setattr(prepare_mod, "check_corpus_freshness", lambda *a, **k: _FakeFreshness(is_fresh=False))
    monkeypatch.setattr(prepare_mod, "BedrockPolicyEmbeddingProvider", lambda **k: object())
    monkeypatch.setattr(prepare_mod, "EmbeddingClient", lambda **k: object())

    def _raise(*a, **k):
        raise RuntimeError("simulated ingestion failure")

    monkeypatch.setattr(prepare_mod, "ingest_corpus", _raise)

    with pytest.raises(RuntimeError, match="simulated ingestion failure"):
        prepare_mod.main()

    assert fake_conn.closed is True
