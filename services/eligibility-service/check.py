"""eligibility-service — payer eligibility check orchestration (X12 270/271).

Stage 1 resilience fix (D4 / RIV-088 / RIV-141): the payer call is now bounded
(timeout + a small number of retries with jittered backoff, in payer_client.py),
guarded by a circuit breaker (breaker.py) so a payer outage fails fast instead
of hanging, and falls back to a Redis-backed last-known-good cache (cache.py)
when the live call can't be completed. A transport failure now maps to
`unknown` (or `stale`, if a cached answer exists) — never `inactive` — per the
EligibilityStatus contract in contracts.py.
"""
from datetime import datetime, timezone
from typing import Callable, Optional

import redis as redis_lib

from breaker import CircuitBreaker
from cache import LastKnownGoodCache
from config import settings
from contracts import EligibilityResult, EligibilityStatus
from errors import CircuitOpenError, RetriesExhaustedError
from payer_client import PayerClient

_redis_client = None


def _redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


_breaker = CircuitBreaker(
    failure_threshold=settings.breaker_failure_threshold,
    reset_timeout_seconds=settings.breaker_reset_timeout_seconds,
)
_client = PayerClient(
    base_url=settings.payer_api_url,
    api_key=settings.payer_api_key,
    timeout_seconds=settings.payer_timeout_seconds,
    max_retries=settings.payer_max_retries,
)


def _cache() -> LastKnownGoodCache:
    return LastKnownGoodCache(
        _redis(),
        fresh_ttl_seconds=settings.cache_fresh_ttl_seconds,
        stale_ttl_seconds=settings.cache_stale_ttl_seconds,
    )


async def check(
    insurance_id: str,
    *,
    client: Optional[PayerClient] = None,
    breaker: Optional[CircuitBreaker] = None,
    cache: Optional[LastKnownGoodCache] = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> EligibilityResult:
    """Check payer eligibility for insurance_id, degrading gracefully.

    `client`/`breaker`/`cache`/`now` are all injectable for tests; production
    callers (app.py) rely on the module-level defaults wired from `settings`.
    """
    client = client if client is not None else _client
    breaker = breaker if breaker is not None else _breaker
    cache_ = cache if cache is not None else _cache()

    if not breaker.allow_request():
        return _fallback(insurance_id, cache_, now, error_type=CircuitOpenError.__name__)

    try:
        raw = await client.check(insurance_id)
    except RetriesExhaustedError as exc:
        breaker.record_failure()
        return _fallback(insurance_id, cache_, now, error_type=type(exc).__name__)

    breaker.record_success()
    status = EligibilityStatus.ACTIVE if raw["active"] else EligibilityStatus.INACTIVE
    result = EligibilityResult(
        insurance_id=insurance_id,
        status=status,
        raw_status=raw.get("raw_status"),
        checked_at=now(),
    )
    cache_.set(result)
    return result


def _fallback(
    insurance_id: str,
    cache_: LastKnownGoodCache,
    now: Callable[[], datetime],
    *,
    error_type: str,
) -> EligibilityResult:
    cached = cache_.get(insurance_id)
    if cached is not None:
        return cached.model_copy(update={"error_type": error_type})
    return EligibilityResult(
        insurance_id=insurance_id,
        status=EligibilityStatus.UNKNOWN,
        checked_at=now(),
        error_type=error_type,
    )
