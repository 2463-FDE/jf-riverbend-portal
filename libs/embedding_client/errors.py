"""Exception hierarchy for the embedding client.

Mirrors libs/llm_client/errors.py: provider adapters raise
ProviderTimeoutError / ProviderTransientError for conditions that are safe to
retry; anything else (e.g. a misconfigured adapter) is left to propagate
as-is so it isn't silently retried.
"""


class EmbeddingClientError(Exception):
    """Base class for all embedding_client errors."""


class ProviderNotConfiguredError(EmbeddingClientError):
    """A provider was selected but is missing required configuration."""


class ProviderTimeoutError(EmbeddingClientError):
    """A single provider call exceeded its allotted timeout. Safe to retry."""


class ProviderTransientError(EmbeddingClientError):
    """A provider call failed in a way that's safe to retry (e.g. connection reset)."""


class EmbeddingRetriesExhaustedError(EmbeddingClientError):
    """All configured retries were used without a successful response."""


class BudgetExceededError(EmbeddingClientError):
    """A configured embedding volume budget would be, or was, exceeded by this call."""
