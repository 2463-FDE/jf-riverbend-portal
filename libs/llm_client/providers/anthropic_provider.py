"""Anthropic (Claude) adapter. API key/model come from environment/config only —
never hardcoded.

The `anthropic` package is imported lazily (inside complete()), so nothing
that merely imports libs.llm_client requires it to be installed — a real
network call only happens if this provider is actually selected and used.
"""
import os

from ..errors import ProviderNotConfiguredError, ProviderTimeoutError, ProviderTransientError
from .base import Provider, ProviderResponse


class AnthropicProvider(Provider):
    def __init__(self, api_key: str = None, model: str = None):
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._model = model or os.getenv("ANTHROPIC_MODEL")
        if not self._api_key or self._api_key == "changeme":
            raise ProviderNotConfiguredError("ANTHROPIC_API_KEY is not configured")
        if not self._model or self._model == "changeme":
            raise ProviderNotConfiguredError("ANTHROPIC_MODEL is not configured")

    def complete(self, prompt: str, *, timeout: float, max_tokens: int) -> ProviderResponse:
        import anthropic  # lazy import — see module docstring

        client = anthropic.Anthropic(api_key=self._api_key, timeout=timeout)
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise ProviderTransientError(str(exc)) from exc

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage = response.usage
        return ProviderResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
        )
