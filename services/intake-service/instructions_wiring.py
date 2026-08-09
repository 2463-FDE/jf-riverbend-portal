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

Codex review (2026-08-08, PR #24, medium): this route used libs/llm_client's
shared defaults (LLM_TIMEOUT_SECONDS=30, LLM_MAX_RETRIES=3) — worst case,
one call could block the request thread for well over 30s while retrying,
but the gateway's own downstream call also times out at a fixed 30s
(services/gateway/app.py::_post). A slow/stalled provider would make the
gateway return a 502 to the browser before this endpoint's own safe template
fallback could ever be returned, and intake-service would keep retrying for
a caller that had already given up. This is only ever an optional,
best-effort helper — a single short, no-retry attempt that falls back to the
deterministic template immediately is strictly better than a slow "real"
answer here. INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS/_MAX_RETRIES (see
.env.example) override just these two fields; provider/model/token-budget
selection still comes from the same shared LLM_PROVIDER/OLLAMA_*/etc vars
every other LLMClient consumer in this repo uses.
"""
import os
from typing import Optional

from libs.llm_client.client import LLMClient, LLMConfig
from libs.safe_logging import get_safe_logger

log = get_safe_logger(__name__)

# Comfortably under the gateway's fixed 30s downstream timeout, with no
# in-client retry — a stalled/slow provider must degrade to the deterministic
# template well before the gateway would otherwise 502 the caller.
_TIMEOUT_SECONDS = float(os.getenv("INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS", "8"))
_MAX_RETRIES = int(os.getenv("INTAKE_INSTRUCTIONS_LLM_MAX_RETRIES", "0"))

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
        _client = LLMClient(LLMConfig(timeout_seconds=_TIMEOUT_SECONDS, max_retries=_MAX_RETRIES))
        return _client
    except Exception as exc:
        log.warning("intake instructions LLM client unavailable (error_type=%s)", type(exc).__name__)
        _client_build_failed = True
        return None
