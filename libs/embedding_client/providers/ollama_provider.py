"""Ollama (local model) embedding adapter. Base URL/model come from
environment/config only.

This is the local/offline default among the real (non-fake) providers: no
vendor API key, no third-party network call, so corpus/query text never
leaves the environment when this provider is selected — the property that
matters for this deliverable's "no PHI leaves the environment" rule. No
cloud embedding provider (OpenAI/Anthropic/Bedrock) is implemented here by
design; see docs/planning/retrieval-eval-seam-map-07-08-2026.md.
"""
import os
from typing import List

import httpx

from ..errors import ProviderNotConfiguredError, ProviderTimeoutError, ProviderTransientError
from .base import EmbeddingProvider, EmbeddingResponse


class OllamaEmbeddingProvider(EmbeddingProvider):
    def __init__(self, base_url: str = None, model: str = None):
        self._base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "")).rstrip("/")
        self._model = model or os.getenv("OLLAMA_EMBED_MODEL")
        if not self._base_url:
            raise ProviderNotConfiguredError("OLLAMA_BASE_URL is not configured")
        if not self._model or self._model == "changeme":
            raise ProviderNotConfiguredError("OLLAMA_EMBED_MODEL is not configured")

    def embed(self, texts: List[str], *, timeout: float) -> EmbeddingResponse:
        vectors: List[List[float]] = []
        input_tokens = 0
        try:
            for text in texts:
                response = httpx.post(
                    f"{self._base_url}/api/embeddings",
                    json={"model": self._model, "prompt": text},
                    timeout=timeout,
                )
                response.raise_for_status()
                body = response.json()
                vectors.append(body.get("embedding", []))
                input_tokens += len(text.split())
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
            raise ProviderTransientError(str(exc)) from exc

        return EmbeddingResponse(vectors=vectors, input_tokens=input_tokens)
