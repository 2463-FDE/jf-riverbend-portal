"""In-memory circuit breaker guarding the payer call.

CLOSED -> OPEN after `failure_threshold` consecutive failures.
OPEN -> HALF_OPEN once `reset_timeout_seconds` has elapsed.
HALF_OPEN -> CLOSED on the next success, or back to OPEN on the next failure.

Process-local only — no cross-worker/replica coordination. That matches
today's single-instance-per-clinic-region deployment (ARCHITECTURE.md); a
multi-replica eligibility-service would need a shared store (e.g. Redis) for
breaker state instead.
"""
import time
from enum import Enum
from typing import Callable, Optional


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._failure_threshold = failure_threshold
        self._reset_timeout_seconds = reset_timeout_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self._reset_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        return self.state != CircuitState.OPEN

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        # Read via the `state` property, not `self._state` directly, so a
        # failure recorded after the reset timeout has elapsed (but before
        # anything else happened to read `.state` first) still correctly
        # reopens from HALF_OPEN rather than only from a stale OPEN.
        if self.state == CircuitState.HALF_OPEN or self._consecutive_failures >= self._failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
