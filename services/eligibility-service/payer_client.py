"""Bounded async payer client (X12 270/271 clearinghouse REST shim).

Stage 1 resilience fix for D4/RIV-088/RIV-141: the payer call now has a
timeout, is retried a small bounded number of times with jittered exponential
backoff, for transport-level failures (timeout / connection reset) AND for a
429 or any 5xx response — those are the payer/clearinghouse telling us it's
rate-limiting or down, not giving a business answer, so they're retried the
same as a transport failure rather than trusted as "coverage is inactive"
(Stage 1 Hardening Fix). Any other completed response (2xx, or a 4xx other
than 429) is treated as a genuine, terminal business answer.
Once retries are exhausted the caller (check.py) decides what to do; this
client never itself decides that a failure means "inactive".

`transport` and `sleep` are injectable so tests never touch the network or a
real clock (mirrors libs/llm_client's injected `sleep` for the same reason).
"""
import asyncio
import random
from typing import Awaitable, Callable, Optional

import httpx

from errors import PayerTimeoutError, PayerTransientError, RetriesExhaustedError

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_SECONDS = 8.0


# 429 (rate limited) and any 5xx (payer/clearinghouse server error) are
# transient — retried the same as a transport failure — never a terminal
# business answer. Everything else (2xx, or a 4xx other than 429) is terminal.
def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


class PayerClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 5.0,
        max_retries: int = 2,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[float, float], float] = random.uniform,
    ):
        self._base_url = base_url
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._transport = transport
        self._sleep = sleep
        self._rand = rand

    async def check(self, insurance_id: str) -> dict:
        """Returns {"insurance_id", "active": bool, "raw_status": int}.

        Raises RetriesExhaustedError (chained from the last classified
        transport or retryable-HTTP-status error) once max_retries is used up.
        """
        params = {"member_id": insurance_id, "service_type": "30"}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_exc: Exception = RetriesExhaustedError("no attempt made")

        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout_seconds) as http_client:
            for attempt in range(self._max_retries + 1):
                try:
                    resp = await http_client.get(self._base_url, params=params, headers=headers)
                except httpx.TimeoutException as exc:
                    last_exc = PayerTimeoutError(type(exc).__name__)
                except httpx.TransportError as exc:
                    last_exc = PayerTransientError(type(exc).__name__)
                else:
                    if _is_retryable_status(resp.status_code):
                        last_exc = PayerTransientError(f"HTTPStatus{resp.status_code}")
                    else:
                        return {
                            "insurance_id": insurance_id,
                            "active": resp.is_success,
                            "raw_status": resp.status_code,
                        }
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_delay(attempt))

        raise RetriesExhaustedError(type(last_exc).__name__) from last_exc

    def _backoff_delay(self, attempt: int) -> float:
        cap = min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_MAX_SECONDS)
        return self._rand(0, cap)
