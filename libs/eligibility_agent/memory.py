"""Visit-scoped structured memory — TTL'd, strictly isolated per visit_id.

Stores ONLY the structured fields VisitContext already defines (insurance_id,
patient_id, eligibility_status, eligibility_checked_at) — never a chat
transcript, prompt, or model response. This is the entire cross-turn
persistence surface for both AgentRuntime implementations; there is nowhere
else conversational content could leak into storage.

Key prefix is separate from every other Redis use in the stack: gateway
sessions use "session:{token}" (services/gateway/security.py), Stage 1's
last-known-good eligibility cache uses "elig:lkg:{insurance_id}"
(services/eligibility-service/cache.py). This uses "agent:visit:{visit_id}".

Best-effort, mirroring the Stage 1 Hardening Fix applied to
services/eligibility-service/cache.py: a memory-store outage must degrade to
"no memory for this visit" (treated the same as a brand-new visit), never an
unhandled exception out of handle_message. Only the error TYPE is logged.
"""
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable, Optional

from libs.safe_logging import get_safe_logger

from .contracts import VisitContext

log = get_safe_logger(__name__)

KEY_PREFIX = "agent:visit:"


class VisitMemoryPort(ABC):
    @abstractmethod
    def get(self, visit_id: str) -> Optional[VisitContext]:
        raise NotImplementedError

    @abstractmethod
    def put(self, context: VisitContext) -> None:
        raise NotImplementedError


class RedisVisitMemory(VisitMemoryPort):
    def __init__(
        self,
        redis_client,
        *,
        ttl_seconds: int = 1800,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds
        self._now = now

    def get(self, visit_id: str) -> Optional[VisitContext]:
        try:
            raw = self._redis.get(self._key(visit_id))
            if not raw:
                return None
            return VisitContext.model_validate_json(raw)
        except Exception as exc:
            log.warning("visit memory read failed (error_type=%s)", type(exc).__name__)
            return None

    def put(self, context: VisitContext) -> None:
        try:
            self._redis.set(self._key(context.visit_id), context.model_dump_json(), ex=self._ttl_seconds)
        except Exception as exc:
            log.warning("visit memory write failed (error_type=%s)", type(exc).__name__)

    @staticmethod
    def _key(visit_id: str) -> str:
        return f"{KEY_PREFIX}{visit_id}"
