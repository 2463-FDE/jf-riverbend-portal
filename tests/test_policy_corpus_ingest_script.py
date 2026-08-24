"""Tests for db/policy_corpus_ingest.py's connection lifecycle (review fix
PN-CONN-LEAK): the embedding provider must be validated BEFORE any Postgres
connection is opened, and once a connection IS opened, any failure in
register_vector or ingest_corpus must still close it.
"""
import pytest

from conftest import load_module

ingest_mod = load_module("db/policy_corpus_ingest.py", "policy_corpus_ingest_script")


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _valid_model_id(monkeypatch):
    monkeypatch.setenv("POLICY_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")


def test_a_missing_aws_region_never_opens_a_postgres_connection(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    calls = []
    monkeypatch.setattr(ingest_mod.psycopg2, "connect", lambda **kw: calls.append(1))

    with pytest.raises(Exception):
        ingest_mod.main()

    assert calls == []


def test_an_ingestion_failure_still_closes_an_already_opened_connection(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fake_conn = _FakeConnection()
    monkeypatch.setattr(ingest_mod.psycopg2, "connect", lambda **kw: fake_conn)
    monkeypatch.setattr(ingest_mod, "register_vector", lambda conn: None)

    def raise_ingestion_error(*args, **kwargs):
        raise RuntimeError("simulated ingestion failure")

    monkeypatch.setattr(ingest_mod, "ingest_corpus", raise_ingestion_error)

    with pytest.raises(RuntimeError, match="simulated ingestion failure"):
        ingest_mod.main()

    assert fake_conn.closed is True
