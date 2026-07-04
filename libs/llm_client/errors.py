"""Exception hierarchy for the LLM client.

Provider adapters raise ProviderTimeoutError / ProviderTransientError for
conditions that are safe to retry; anything else (e.g. a misconfigured
adapter, an auth failure) is left to propagate as-is so it isn't silently
retried.
"""


class LLMClientError(Exception):
    """Base class for all llm_client errors."""


class ProviderNotConfiguredError(LLMClientError):
    """A provider was selected but is missing required configuration (e.g. an API key)."""


class ProviderTimeoutError(LLMClientError):
    """A single provider call exceeded its allotted timeout. Safe to retry."""


class ProviderTransientError(LLMClientError):
    """A provider call failed in a way that's safe to retry (e.g. rate limit, connection reset)."""


class LLMRetriesExhaustedError(LLMClientError):
    """All configured retries were used without a successful response."""


class StructuredOutputError(LLMClientError):
    """The provider's text output did not validate against the requested schema."""


class BudgetExceededError(LLMClientError):
    """A configured token/cost budget would be, or was, exceeded by this call."""
