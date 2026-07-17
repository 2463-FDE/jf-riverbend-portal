"""Redis-backed last-known-good eligibility cache.

Separate key prefix from any other Redis use in the stack (gateway sessions
use "session:{token}" — services/gateway/security.py). Only ever caches a
successful, terminal payer result (active/inactive); a transport failure,
"unknown", "pending", or "stale" result is never written here, so a cache hit
always reflects a real past payer answer, not a fabricated one.

Reads distinguish three cases:
  * within `fresh_ttl_seconds`  -> returned as-is (status active/inactive)
  * older, but within `stale_ttl_seconds` -> returned with status STALE, the
    original status in `cached_status`, and its age in `stale_age_seconds`
  * missing (never cached, or past `stale_ttl_seconds` and expired out of
    Redis via the key's own TTL) -> None
"""
import json
from datetime import datetime, timezone
from typing import Callable, Optional

from contracts import EligibilityResult, EligibilityStatus

KEY_PREFIX = "elig:lkg:"
_CACHEABLE_STATUSES = frozenset({EligibilityStatus.ACTIVE, EligibilityStatus.INACTIVE})


class LastKnownGoodCache:
    def __init__(
        self,
        redis_client,
        *,
        fresh_ttl_seconds: int = 300,
        stale_ttl_seconds: int = 3600,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if stale_ttl_seconds < fresh_ttl_seconds:
            raise ValueError("stale_ttl_seconds must be >= fresh_ttl_seconds")
        self._redis = redis_client
        self._fresh_ttl_seconds = fresh_ttl_seconds
        self._stale_ttl_seconds = stale_ttl_seconds
        self._now = now

    def set(self, result: EligibilityResult) -> None:
        if result.status not in _CACHEABLE_STATUSES:
            return
        payload = {
            "status": result.status.value,
            "payer": result.payer,
            "raw_status": result.raw_status,
            "checked_at": result.checked_at.isoformat(),
        }
        self._redis.set(self._key(result.insurance_id), json.dumps(payload), ex=self._stale_ttl_seconds)

    def get(self, insurance_id: str) -> Optional[EligibilityResult]:
        raw = self._redis.get(self._key(insurance_id))
        if not raw:
            return None
        payload = json.loads(raw)
        checked_at = datetime.fromisoformat(payload["checked_at"])
        age_seconds = (self._now() - checked_at).total_seconds()

        if age_seconds <= self._fresh_ttl_seconds:
            return EligibilityResult(
                insurance_id=insurance_id,
                status=EligibilityStatus(payload["status"]),
                payer=payload.get("payer"),
                raw_status=payload.get("raw_status"),
                checked_at=checked_at,
            )

        return EligibilityResult(
            insurance_id=insurance_id,
            status=EligibilityStatus.STALE,
            payer=payload.get("payer"),
            raw_status=payload.get("raw_status"),
            checked_at=checked_at,
            cached_status=EligibilityStatus(payload["status"]),
            stale_age_seconds=age_seconds,
        )

    @staticmethod
    def _key(insurance_id: str) -> str:
        return f"{KEY_PREFIX}{insurance_id}"
