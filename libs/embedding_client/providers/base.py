"""Provider interface for the embedding client — the seam that makes it
provider-swappable. Deliberately a separate interface from
libs/llm_client/providers/base.py's `Provider` (completion-shaped: one prompt
in, one text out): embedding is batch-shaped (many texts in, many vectors
out) and has no prompt/schema semantics, so forcing it through the
completion `Provider` interface would be the wrong abstraction, not reuse.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class EmbeddingResponse:
    vectors: List[List[float]]
    input_tokens: int


class EmbeddingProvider(ABC):
    """A single-method adapter to one embedding backend.

    Implementations must translate their own transport/timeout errors into
    ProviderTimeoutError or ProviderTransientError so the client's retry logic
    knows what's safe to retry, and let anything else (e.g. auth failures)
    propagate unchanged.
    """

    @abstractmethod
    def embed(self, texts: List[str], *, timeout: float) -> EmbeddingResponse:
        raise NotImplementedError
