"""Provider-swappable embedding client: bounded timeout, retry with
exponential backoff and jitter, and a volume/cost guard.

Provider selection is config-only (EMBEDDING_PROVIDER=fake|ollama). No cloud
embedding provider (OpenAI/Anthropic/Bedrock) is wired up by design — corpus
and query text must not leave the environment for this deliverable. See
client.py for EmbeddingClient / EmbeddingConfig and providers/ for the
adapters.
"""
from .client import EmbeddingClient, EmbeddingConfig
from .errors import (
    BudgetExceededError,
    EmbeddingClientError,
    EmbeddingRetriesExhaustedError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    ProviderTransientError,
)

__all__ = [
    "EmbeddingClient",
    "EmbeddingConfig",
    "EmbeddingClientError",
    "EmbeddingRetriesExhaustedError",
    "BudgetExceededError",
    "ProviderNotConfiguredError",
    "ProviderTimeoutError",
    "ProviderTransientError",
]
