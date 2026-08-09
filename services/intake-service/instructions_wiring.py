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

Codex review (2026-08-09, PR #24): INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS/
_MAX_RETRIES used to be parsed with a bare float()/int() at MODULE IMPORT
TIME — a malformed value (e.g. "eight") raised before get_llm_client() ever
ran, and app.py imports this module at process startup, so a typo in this
OPTIONAL feature's config could crash intake-service entirely, taking core
patient registration down with it. Parsing now happens lazily, inside
get_llm_client()'s own guarded call, and a malformed value logs a warning
and falls back to the safe default rather than propagating — this module can
no longer prevent the service from starting, period.
"""
import os
from typing import Optional

from libs.llm_client.client import LLMClient, LLMConfig
from libs.safe_logging import get_safe_logger

log = get_safe_logger(__name__)

# Comfortably under the gateway's fixed 30s downstream timeout, with no
# in-client retry — a stalled/slow provider must degrade to the deterministic
# template well before the gateway would otherwise 502 the caller.
_DEFAULT_TIMEOUT_SECONDS = 8.0
_DEFAULT_MAX_RETRIES = 0

_client: Optional[LLMClient] = None
_client_build_failed = False


def _bounded_timeout_seconds() -> float:
    raw = os.getenv("INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "invalid INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS=%r — using default %s",
            raw,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS


def _bounded_max_retries() -> int:
    raw = os.getenv("INTAKE_INSTRUCTIONS_LLM_MAX_RETRIES")
    if raw is None:
        return _DEFAULT_MAX_RETRIES
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "invalid INTAKE_INSTRUCTIONS_LLM_MAX_RETRIES=%r — using default %s",
            raw,
            _DEFAULT_MAX_RETRIES,
        )
        return _DEFAULT_MAX_RETRIES


def get_llm_client() -> Optional[LLMClient]:
    """Memoized `LLMClient`, or `None` if it could not be built. Never
    raises — a construction failure (e.g. `ProviderNotConfiguredError`, or a
    malformed timeout/retry env var) is logged (TYPE only, or the invalid raw
    value for a config typo — never patient data) once and remembered. `None`
    makes `libs.intake_instructions.composer.compose` skip the model and
    return its deterministic per-step template instead."""
    global _client, _client_build_failed
    if _client is not None:
        return _client
    if _client_build_failed:
        return None
    try:
        _client = LLMClient(LLMConfig(timeout_seconds=_bounded_timeout_seconds(), max_retries=_bounded_max_retries()))
        return _client
    except Exception as exc:
        log.warning("intake instructions LLM client unavailable (error_type=%s)", type(exc).__name__)
        _client_build_failed = True
        return None
