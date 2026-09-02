"""EmbeddingClient.tokens_used / .retry_count (W10 Metrics Stage 5) — the
"provider retry and embedding input-token totals" libs/rag_eval_metrics
reads off a client instance after an evaluation run.
"""
from libs.embedding_client import EmbeddingClient
from libs.embedding_client.errors import ProviderTransientError
from libs.embedding_client.providers.fake_provider import FakeEmbeddingProvider


def _client(script=None, sleep=lambda s: None):
    return EmbeddingClient(provider=FakeEmbeddingProvider(script), sleep=sleep)


def test_tokens_used_starts_at_zero():
    assert _client().tokens_used == 0


def test_tokens_used_accumulates_across_multiple_embed_calls():
    client = _client()
    client.embed(["one two three"])  # 3 tokens
    client.embed(["four five"])  # 2 tokens
    assert client.tokens_used == 5


def test_retry_count_is_zero_when_every_call_succeeds_first_try():
    client = _client()
    client.embed(["hello"])
    assert client.retry_count == 0


def test_retry_count_increments_once_per_retried_attempt_not_per_call():
    client = _client(script=[ProviderTransientError("boom"), None])
    client.embed(["hello"])
    assert client.retry_count == 1


def test_retry_count_accumulates_across_separate_embed_calls():
    client = _client(script=[ProviderTransientError("boom"), None, ProviderTransientError("boom"), None])
    client.embed(["first call"])
    client.embed(["second call"])
    assert client.retry_count == 2
