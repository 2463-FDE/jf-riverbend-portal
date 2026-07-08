"""Deterministic, no-network embedding provider for tests and as the safe
default. Never makes a real API call.

Produces a fixed-dimension vector derived from each input text's SHA-256
hash, so the same text always embeds to the same vector across runs and
processes — this is what lets the embedding-cache pipeline (libs/rag_corpus)
and its tests exercise "second run must not re-embed unchanged records"
without a real embedding backend.
"""
import hashlib
from collections import deque
from typing import Deque, List, Optional, Union

from .base import EmbeddingProvider, EmbeddingResponse

_DIMENSIONS = 16

ScriptItem = Union[Exception, None]


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, script: Optional[List[ScriptItem]] = None):
        # An optional queue of exceptions to raise on successive calls (None =
        # succeed normally that call), mirroring FakeProvider in
        # libs/llm_client/providers/fake_provider.py — lets tests exercise
        # retry/backoff without touching a real provider.
        self._script: Deque[ScriptItem] = deque(script if script is not None else [])
        self.calls: List[dict] = []

    def embed(self, texts: List[str], *, timeout: float) -> EmbeddingResponse:
        self.calls.append({"count": len(texts), "timeout": timeout})
        if self._script:
            item = self._script.popleft()
            if isinstance(item, Exception):
                raise item
        vectors = [self._deterministic_vector(text) for text in texts]
        input_tokens = sum(len(text.split()) for text in texts)
        return EmbeddingResponse(vectors=vectors, input_tokens=input_tokens)

    @staticmethod
    def _deterministic_vector(text: str) -> List[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[:_DIMENSIONS]]
