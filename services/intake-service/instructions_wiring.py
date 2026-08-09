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

Codex review (2026-08-09, PR #24, second round on this file): parsing
successfully is not the same as being safe. A syntactically valid but
oversized/negative/non-finite value (e.g. 60, -1, "nan", "inf") used to pass
straight through — 60s alone already exceeds the gateway's fixed 30s
downstream timeout (services/gateway/app.py::_post), reintroducing the exact
"gateway 502s before the template can return, worker keeps retrying for a
gone caller" failure this config was added to prevent, and NaN/Infinity as
an httpx timeout is undefined/unbounded behavior. `_bounded_timeout_seconds`/
`_bounded_max_retries` now also range-check the parsed value — out of range
degrades to the default exactly like a parse failure does. `_WORST_CASE_...`
below computes the worst-case total call duration these bounds allow
((max_retries + 1) attempts at the timeout ceiling, plus a capped backoff
sleep between each) and asserts it at import time against the gateway's
fixed budget, so a careless future change to either ceiling fails loudly
here instead of silently reopening this exact defect.
"""
import math
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

# Hard ceilings an operator-configured value may never exceed, regardless of
# how it parses — see the module docstring's second Codex-review note.
_MAX_ALLOWED_TIMEOUT_SECONDS = 10.0
_MAX_ALLOWED_RETRIES = 1

# services/gateway/app.py::_post's hardcoded downstream call timeout.
_GATEWAY_DOWNSTREAM_TIMEOUT_SECONDS = 30.0
# libs/llm_client/client.py::_BACKOFF_MAX_SECONDS — the largest possible
# sleep LLMClient inserts between retry attempts.
_LLM_CLIENT_MAX_BACKOFF_SECONDS = 8.0

_WORST_CASE_CALL_DURATION_SECONDS = (
    (_MAX_ALLOWED_RETRIES + 1) * _MAX_ALLOWED_TIMEOUT_SECONDS
    + _MAX_ALLOWED_RETRIES * _LLM_CLIENT_MAX_BACKOFF_SECONDS
)
assert _WORST_CASE_CALL_DURATION_SECONDS < _GATEWAY_DOWNSTREAM_TIMEOUT_SECONDS, (
    "intake instructions' worst-case LLM call duration must stay under the "
    "gateway's downstream timeout — tighten _MAX_ALLOWED_TIMEOUT_SECONDS "
    "and/or _MAX_ALLOWED_RETRIES above"
)

_client: Optional[LLMClient] = None
_client_build_failed = False


def _bounded_timeout_seconds() -> float:
    raw = os.getenv("INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "invalid INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS=%r — using default %s",
            raw,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(value) or not (0 < value <= _MAX_ALLOWED_TIMEOUT_SECONDS):
        log.warning(
            "INTAKE_INSTRUCTIONS_LLM_TIMEOUT_SECONDS=%r is outside the safe range "
            "(0, %s] — using default %s",
            raw,
            _MAX_ALLOWED_TIMEOUT_SECONDS,
            _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS
    return value


def _bounded_max_retries() -> int:
    raw = os.getenv("INTAKE_INSTRUCTIONS_LLM_MAX_RETRIES")
    if raw is None:
        return _DEFAULT_MAX_RETRIES
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "invalid INTAKE_INSTRUCTIONS_LLM_MAX_RETRIES=%r — using default %s",
            raw,
            _DEFAULT_MAX_RETRIES,
        )
        return _DEFAULT_MAX_RETRIES
    if not (0 <= value <= _MAX_ALLOWED_RETRIES):
        log.warning(
            "INTAKE_INSTRUCTIONS_LLM_MAX_RETRIES=%r is outside the safe range "
            "[0, %s] — using default %s",
            raw,
            _MAX_ALLOWED_RETRIES,
            _DEFAULT_MAX_RETRIES,
        )
        return _DEFAULT_MAX_RETRIES
    return value


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
