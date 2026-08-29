"""Explicit payer integration mode (W10 Final Stage 1, RIV-088/141 follow-up).

Replaces the old implicit rule "PAYER_API_KEY is blank => simulate": that
inference lived only in the gateway's coverage-verify route, so a caller
reaching eligibility-service directly (its host port is published, same as
every other domain service — see ARCHITECTURE.md's known gap) had no
equivalent guard and would run the real payer client against
services/eligibility-service/config.py's shipped example URL. `check.py`
now enforces this itself, at the one place the payer is actually called.
"""
from typing import Optional

# The example domain shipped in config.py's PAYER_API_URL default. No real
# payer is ever reachable at it; seeing it configured for 'live' mode means
# the operator never set a real endpoint, not that this is a legitimate payer.
_PLACEHOLDER_PAYER_HOSTS = ("edi.example.com",)

VALID_MODES = ("simulation", "live")


class PayerModeConfigError(ValueError):
    """Raised for an unrecognized mode, or a 'live' mode missing/placeholder
    credential or endpoint configuration. Never carries the credential value
    itself — only which setting is missing or still a placeholder."""


def validate(mode: str, *, api_key: str, api_url: str) -> None:
    if mode not in VALID_MODES:
        raise PayerModeConfigError(
            f"PAYER_INTEGRATION_MODE must be one of {VALID_MODES}, got {mode!r}"
        )
    if mode != "live":
        return
    if not api_key:
        raise PayerModeConfigError("PAYER_INTEGRATION_MODE=live requires PAYER_API_KEY to be set")
    if not api_url or any(host in api_url for host in _PLACEHOLDER_PAYER_HOSTS):
        raise PayerModeConfigError(
            "PAYER_INTEGRATION_MODE=live requires a real PAYER_API_URL, not the "
            "shipped example endpoint"
        )


def config_error(mode: str, *, api_key: str, api_url: str) -> Optional[str]:
    """Same check as validate(), returned as a message instead of raised —
    for a call site (check.py) that wants to degrade gracefully rather than
    propagate an exception through an async eligibility check."""
    try:
        validate(mode, api_key=api_key, api_url=api_url)
    except PayerModeConfigError as exc:
        return str(exc)
    return None
