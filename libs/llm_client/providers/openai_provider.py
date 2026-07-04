"""OpenAI adapter. API key/model come from environment/config only — never hardcoded.

The `openai` package is imported lazily (inside complete()), so nothing that
merely imports libs.llm_client requires it to be installed — a real network
call only happens if this provider is actually selected and used.
"""
import os

from ..errors import ProviderNotConfiguredError, ProviderTimeoutError, ProviderTransientError
from .base import Provider, ProviderResponse


class OpenAIProvider(Provider):
    def __init__(self, api_key: str = None, model: str = None):
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._model = model or os.getenv("OPENAI_MODEL")
        if not self._api_key or self._api_key == "changeme":
            raise ProviderNotConfiguredError("OPENAI_API_KEY is not configured")
        if not self._model or self._model == "changeme":
            raise ProviderNotConfiguredError("OPENAI_MODEL is not configured")

    def complete(self, prompt: str, *, timeout: float, max_tokens: int) -> ProviderResponse:
        import openai  # lazy import — see module docstring

        client = openai.OpenAI(api_key=self._api_key, timeout=timeout)
        try:
            response = client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
        except openai.APITimeoutError as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except (openai.RateLimitError, openai.APIConnectionError) as exc:
            raise ProviderTransientError(str(exc)) from exc

        choice = response.choices[0]
        usage = response.usage
        return ProviderResponse(
            text=choice.message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", 0),
            output_tokens=getattr(usage, "completion_tokens", 0),
        )
