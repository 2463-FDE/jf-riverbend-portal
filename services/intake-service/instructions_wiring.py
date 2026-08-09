"""Lazy, safe construction of the Stage 1 (feature-readiness) `LLMClient` for
`POST /intake/instructions`.

Mirrors eligibility-service's `agent_wiring.py::get_agent_runtime`: built
once, on first use, and memoized, so a misconfigured/unavailable provider
(e.g. `LLM_PROVIDER=ollama` with no local server running, or a real vendor
provider with no credential) is only ever attempted once per process rather
than retried on every request. `LLM_PROVIDER` defaults to `fake` (see
libs/llm_client/client.py::LLMConfig), which always constructs successfully
and never makes a network call — the memoized-failure path here only matters
for a deployment that has actually opted into a real provider.
"""
from typing import Optional

from libs.llm_client.client import LLMClient, LLMConfig
from libs.safe_logging import get_safe_logger

log = get_safe_logger(__name__)

_client: Optional[LLMClient] = None
_client_build_failed = False


def get_llm_client() -> Optional[LLMClient]:
    """Memoized `LLMClient`, or `None` if it could not be built. Never
    raises — a construction failure (e.g. `ProviderNotConfiguredError`) is
    logged (TYPE only) once and remembered. `None` makes
    `libs.intake_instructions.composer.compose` skip the model and return
    its deterministic per-step template instead."""
    global _client, _client_build_failed
    if _client is not None:
        return _client
    if _client_build_failed:
        return None
    try:
        _client = LLMClient(LLMConfig())
        return _client
    except Exception as exc:
        log.warning("intake instructions LLM client unavailable (error_type=%s)", type(exc).__name__)
        _client_build_failed = True
        return None
