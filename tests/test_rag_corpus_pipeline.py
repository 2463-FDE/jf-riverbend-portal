"""Tests for the capped corpus builder + embed-once-and-cache pipeline
(libs/rag_corpus). Uses only the fake embedding provider — never a real
network call — and asserts the specific cost failure mode this pipeline
exists to avoid: re-embedding unchanged records on a repeat run.
"""
import logging

from libs.embedding_client import EmbeddingClient, EmbeddingConfig
from libs.embedding_client.providers.fake_provider import FakeEmbeddingProvider
from libs.rag_corpus import CorpusConfig, CorpusRecord, EmbeddingCache, build_corpus, run_pipeline

PHI_MARKER = "ssn=111-22-3333"


def _client(provider):
    return EmbeddingClient(config=EmbeddingConfig(provider="fake"), provider=provider)


def _record(record_id, patient_id, text="fixed fixture text"):
    return CorpusRecord(
        record_id=record_id,
        patient_id=patient_id,
        patient_name="seed fixture",
        text=text,
        occurred_at="2026-01-01 00:00:00",
    )


def _log_text(caplog):
    return "\n".join(record.getMessage() for record in caplog.records)


# --- cache-hit path: never re-embed unchanged records ------------------------


def test_second_pipeline_run_over_unchanged_records_makes_no_new_embedding_calls(tmp_path):
    provider = FakeEmbeddingProvider()
    client = _client(provider)
    config = CorpusConfig(max_records=3, cache_dir=str(tmp_path))

    first = run_pipeline(config=config, embedding_client=client)
    assert first.newly_embedded == len(first.corpus)
    assert first.cache_hits == 0
    calls_after_first_run = len(provider.calls)
    assert calls_after_first_run > 0

    second = run_pipeline(config=config, embedding_client=client)

    assert second.newly_embedded == 0
    assert second.cache_hits == len(second.corpus)
    assert len(provider.calls) == calls_after_first_run  # no additional embed() calls
    assert second.vectors_by_record_id == first.vectors_by_record_id


def test_embedding_cache_persists_to_disk_across_separate_cache_instances(tmp_path):
    # run_pipeline() builds a fresh EmbeddingCache object each call, so the
    # "no re-embed" guarantee must hold via the on-disk cache file, not
    # in-memory state on a single EmbeddingCache instance.
    provider = FakeEmbeddingProvider()
    client = _client(provider)
    records = [_record("r1", 1), _record("r2", 2)]

    EmbeddingCache(cache_dir=str(tmp_path), provider="fake").get_or_embed_all(records, client)
    calls_after_first_load = len(provider.calls)

    vectors, hits, embedded = EmbeddingCache(cache_dir=str(tmp_path), provider="fake").get_or_embed_all(
        records, client
    )

    assert hits == 2
    assert embedded == 0
    assert len(provider.calls) == calls_after_first_load


def test_only_records_with_changed_text_are_reembedded(tmp_path):
    provider = FakeEmbeddingProvider()
    client = _client(provider)
    cache_dir = str(tmp_path)

    records_v1 = [_record("r1", 1, "version one"), _record("r2", 2, "stable text")]
    vectors_v1, hits_v1, embedded_v1 = EmbeddingCache(cache_dir=cache_dir, provider="fake").get_or_embed_all(
        records_v1, client
    )
    assert (hits_v1, embedded_v1) == (0, 2)
    calls_after_v1 = len(provider.calls)

    records_v2 = [_record("r1", 1, "version two — changed"), _record("r2", 2, "stable text")]
    vectors_v2, hits_v2, embedded_v2 = EmbeddingCache(cache_dir=cache_dir, provider="fake").get_or_embed_all(
        records_v2, client
    )

    assert hits_v2 == 1  # r2's text is unchanged
    assert embedded_v2 == 1  # only r1's changed text triggers a new embed call
    assert len(provider.calls) == calls_after_v1 + 1
    assert vectors_v2["r2"] == vectors_v1["r2"]
    assert vectors_v2["r1"] != vectors_v1["r1"]


# --- fake/mocked provider only; no real API calls ----------------------------


def test_pipeline_never_invokes_a_real_provider(tmp_path):
    provider = FakeEmbeddingProvider()
    client = _client(provider)
    config = CorpusConfig(max_records=2, cache_dir=str(tmp_path))

    result = run_pipeline(config=config, embedding_client=client)

    assert client.provider_name == "fake"
    assert result.newly_embedded == len(result.corpus)
    # every embed() call landed on the fake provider, not a network client
    assert all("count" in call for call in provider.calls)


# --- redaction / no-PHI-logging on the corpus + embedding path --------------


def test_build_corpus_does_not_log_patient_names_or_record_text(caplog):
    caplog.set_level(logging.INFO, logger="libs.rag_corpus.corpus")

    records = build_corpus(max_records=10)

    text = _log_text(caplog)
    assert text  # sanity: build_corpus does log something (counts)
    for record in records:
        assert record.patient_name not in text
        assert record.text not in text


def test_embedding_cache_does_not_log_record_text_or_vectors(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    provider = FakeEmbeddingProvider()
    client = _client(provider)
    record_text = f"chart note containing {PHI_MARKER}"
    records = [_record("r1", 1, record_text)]

    vectors, _, _ = EmbeddingCache(cache_dir=str(tmp_path), provider="fake").get_or_embed_all(records, client)

    text = _log_text(caplog)
    assert PHI_MARKER not in text
    assert record_text not in text
    assert str(vectors["r1"]) not in text
