"""Provider-swappable embedding client: bounded timeout, retry with
exponential backoff and jitter, and a volume/cost guard — mirrors
libs/llm_client's design (LLMClient/LLMConfig) but embeds instead of
completes, since forcing embeddings through a chat-completion interface
would be the wrong shape: batch input/output, no prompt or structured-output
semantics. Provider selection is config-only (EMBEDDING_PROVIDER); nothing
here ever hardcodes a credential.
"""
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from libs.safe_logging import get_safe_logger

from .errors import (
    BudgetExceededError,
    EmbeddingRetriesExhaustedError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from .providers.base import EmbeddingProvider

log = get_safe_logger(__name__)

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 8.0


@dataclass
class EmbeddingConfig:
    provider: str = field(default_factory=lambda: os.getenv("EMBEDDING_PROVIDER", "fake"))
    timeout_seconds: float = field(default_factory=lambda: float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "30")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("EMBEDDING_MAX_RETRIES", "3")))
    monthly_token_budget: int = field(
        default_factory=lambda: int(os.getenv("EMBEDDING_MONTHLY_TOKEN_BUDGET", "1000000"))
    )


def _build_provider(name: str) -> EmbeddingProvider:
    if name == "fake":
        from .providers.fake_provider import FakeEmbeddingProvider

        return FakeEmbeddingProvider()
    if name == "ollama":
        from .providers.ollama_provider import OllamaEmbeddingProvider

        return OllamaEmbeddingProvider()
    raise ValueError(
        f"Unknown EMBEDDING_PROVIDER '{name}' — expected one of: fake, ollama "
        "(no cloud embedding provider is wired up by design for this deliverable)"
    )


class EmbeddingClient:
    def __init__(
        self,
        config: Optional[EmbeddingConfig] = None,
        provider: Optional[EmbeddingProvider] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._config = config or EmbeddingConfig()
        self._provider = provider if provider is not None else _build_provider(self._config.provider)
        self._sleep = sleep
        self._tokens_used = 0

    @property
    def provider_name(self) -> str:
        return self._config.provider

    def embed(self, texts: List[str], *, timeout: Optional[float] = None) -> List[List[float]]:
        if not texts:
            return []
        timeout = timeout if timeout is not None else self._config.timeout_seconds

        if self._tokens_used >= self._config.monthly_token_budget:
            raise BudgetExceededError(
                f"Monthly embedding token budget ({self._config.monthly_token_budget}) already reached"
            )

        response = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._provider.embed(texts, timeout=timeout)
                break
            except (ProviderTimeoutError, ProviderTransientError) as exc:
                # Log the exception TYPE only, never str(exc) — see
                # docs/planning/phi-safe-logging-policy.md rule 5.
                if attempt >= self._config.max_retries:
                    log.error(
                        "embedding_client retries exhausted (provider=%s, attempts=%s, error_type=%s)",
                        self._config.provider,
                        attempt + 1,
                        type(exc).__name__,
                    )
                    raise EmbeddingRetriesExhaustedError(
                        f"Gave up after {attempt + 1} attempt(s): {type(exc).__name__}"
                    ) from exc
                log.warning(
                    "embedding_client retrying (provider=%s, attempt=%s/%s, error_type=%s)",
                    self._config.provider,
                    attempt + 1,
                    self._config.max_retries,
                    type(exc).__name__,
                )
                self._sleep(self._backoff_delay(attempt))

        self._tokens_used += response.input_tokens
        if self._tokens_used > self._config.monthly_token_budget:
            log.error(
                "embedding_client budget exceeded (provider=%s, tokens_used=%s, budget=%s)",
                self._config.provider,
                self._tokens_used,
                self._config.monthly_token_budget,
            )
            raise BudgetExceededError(
                f"This call pushed usage to {self._tokens_used} tokens, over the "
                f"monthly budget of {self._config.monthly_token_budget}"
            )

        log.info(
            "embedding_client embed ok (provider=%s, count=%s, input_tokens=%s)",
            self._config.provider,
            len(texts),
            response.input_tokens,
        )
        return response.vectors

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        cap = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS)
        return random.uniform(0, cap)
