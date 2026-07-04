"""Provider interface for the LLM client — the seam that makes it provider-swappable."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int


class Provider(ABC):
    """A single-method adapter to one LLM backend.

    Implementations must translate their own transport/timeout errors into
    ProviderTimeoutError or ProviderTransientError so the client's retry logic
    knows what's safe to retry, and let anything else (e.g. auth failures)
    propagate unchanged.
    """

    @abstractmethod
    def complete(self, prompt: str, *, timeout: float, max_tokens: int) -> ProviderResponse:
        raise NotImplementedError
