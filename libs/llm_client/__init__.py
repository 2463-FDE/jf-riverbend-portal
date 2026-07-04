"""Provider-swappable LLM client: bounded timeout, retry with exponential
backoff and jitter, structured-output parsing, and a token/cost guard.

Provider selection is config-only (LLM_PROVIDER=openai|anthropic|ollama|fake).
See client.py for LLMClient / LLMConfig and providers/ for the adapters.
"""
from .client import LLMClient, LLMConfig
from .errors import (
    BudgetExceededError,
    LLMClientError,
    LLMRetriesExhaustedError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    ProviderTransientError,
    StructuredOutputError,
)

__all__ = [
    "LLMClient",
    "LLMConfig",
    "LLMClientError",
    "LLMRetriesExhaustedError",
    "StructuredOutputError",
    "BudgetExceededError",
    "ProviderNotConfiguredError",
    "ProviderTimeoutError",
    "ProviderTransientError",
]
