"""Provider-swappable LLM client: bounded timeout, retry with exponential
backoff and jitter, structured-output parsing, and a token/cost guard — all
provider-agnostic. Provider selection is config-only (LLM_PROVIDER); nothing
here ever hardcodes a credential.
"""
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Type, TypeVar, Union

from pydantic import BaseModel

from .errors import (
    BudgetExceededError,
    LLMRetriesExhaustedError,
    ProviderTimeoutError,
    ProviderTransientError,
    StructuredOutputError,
)
from .providers.base import Provider

T = TypeVar("T", bound=BaseModel)

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 8.0


@dataclass
class LLMConfig:
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "fake"))
    timeout_seconds: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_SECONDS", "30")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_RETRIES", "3")))
    max_tokens_per_request: int = field(
        default_factory=lambda: int(os.getenv("LLM_MAX_TOKENS_PER_REQUEST", "2000"))
    )
    monthly_token_budget: int = field(
        default_factory=lambda: int(os.getenv("LLM_MONTHLY_TOKEN_BUDGET", "1000000"))
    )


def _build_provider(name: str) -> Provider:
    if name == "fake":
        from .providers.fake_provider import FakeProvider

        return FakeProvider()
    if name == "openai":
        from .providers.openai_provider import OpenAIProvider

        return OpenAIProvider()
    if name == "anthropic":
        from .providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if name == "ollama":
        from .providers.ollama_provider import OllamaProvider

        return OllamaProvider()
    raise ValueError(f"Unknown LLM_PROVIDER '{name}' — expected one of: fake, openai, anthropic, ollama")


class LLMClient:
    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        provider: Optional[Provider] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._config = config or LLMConfig()
        self._provider = provider if provider is not None else _build_provider(self._config.provider)
        self._sleep = sleep
        self._tokens_used = 0

    def complete(
        self,
        prompt: str,
        *,
        schema: Optional[Type[T]] = None,
        timeout: Optional[float] = None,
    ) -> Union[str, T]:
        timeout = timeout if timeout is not None else self._config.timeout_seconds

        if self._tokens_used >= self._config.monthly_token_budget:
            raise BudgetExceededError(
                f"Monthly token budget ({self._config.monthly_token_budget}) already reached"
            )

        response = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._provider.complete(
                    prompt, timeout=timeout, max_tokens=self._config.max_tokens_per_request
                )
                break
            except (ProviderTimeoutError, ProviderTransientError) as exc:
                if attempt >= self._config.max_retries:
                    raise LLMRetriesExhaustedError(
                        f"Gave up after {attempt + 1} attempt(s): {exc}"
                    ) from exc
                self._sleep(self._backoff_delay(attempt))

        self._tokens_used += response.input_tokens + response.output_tokens
        if self._tokens_used > self._config.monthly_token_budget:
            raise BudgetExceededError(
                f"This call pushed usage to {self._tokens_used} tokens, over the "
                f"monthly budget of {self._config.monthly_token_budget}"
            )

        if schema is not None:
            try:
                return schema.model_validate_json(response.text)
            except Exception as exc:
                raise StructuredOutputError(str(exc)) from exc

        return response.text

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        cap = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS)
        return random.uniform(0, cap)
