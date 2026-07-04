"""Ollama (local model) adapter. Base URL/model come from environment/config only.

Ollama is a local server, not a hosted vendor — there's no API key to keep
secret — but the base URL and model name still come from config rather than
being hardcoded, and no real HTTP call happens unless this provider is
actually selected.
"""
import os

import httpx

from ..errors import ProviderNotConfiguredError, ProviderTimeoutError, ProviderTransientError
from .base import Provider, ProviderResponse


class OllamaProvider(Provider):
    def __init__(self, base_url: str = None, model: str = None):
        self._base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "")).rstrip("/")
        self._model = model or os.getenv("OLLAMA_MODEL")
        if not self._base_url:
            raise ProviderNotConfiguredError("OLLAMA_BASE_URL is not configured")
        if not self._model or self._model == "changeme":
            raise ProviderNotConfiguredError("OLLAMA_MODEL is not configured")

    def complete(self, prompt: str, *, timeout: float, max_tokens: int) -> ProviderResponse:
        try:
            response = httpx.post(
                f"{self._base_url}/api/generate",
                json={
                    "model": self._model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
            raise ProviderTransientError(str(exc)) from exc

        body = response.json()
        return ProviderResponse(
            text=body.get("response", ""),
            input_tokens=body.get("prompt_eval_count", 0),
            output_tokens=body.get("eval_count", 0),
        )
