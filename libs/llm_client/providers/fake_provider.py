"""Deterministic, no-network provider for tests and as the safe default.

Never makes a real API call. Scripted with a fixed sequence of responses
and/or exceptions to return, in order — lets tests exercise retry/backoff,
timeout, and structured-output behavior without touching a real provider.
"""
from collections import deque
from typing import Deque, List, Optional, Union

from .base import Provider, ProviderResponse

ScriptItem = Union[ProviderResponse, Exception]


class FakeProvider(Provider):
    def __init__(self, script: Optional[List[ScriptItem]] = None):
        default: List[ScriptItem] = [ProviderResponse(text="{}", input_tokens=1, output_tokens=1)]
        self._script: Deque[ScriptItem] = deque(script if script is not None else default)
        self.calls: List[dict] = []

    def complete(self, prompt: str, *, timeout: float, max_tokens: int) -> ProviderResponse:
        self.calls.append({"prompt": prompt, "timeout": timeout, "max_tokens": max_tokens})
        if not self._script:
            raise RuntimeError("FakeProvider script exhausted — add more scripted responses")
        item = self._script.popleft()
        if isinstance(item, Exception):
            raise item
        return item
