"""Explicit, versioned Bedrock chat pricing — W10 Metrics Stage 4.

Rates are hardcoded here, dated, and versioned deliberately: this project
does not scrape live vendor pricing at startup or in CI (per the stage
spec), and a wrong guess would silently misprice every usage row that
matched it. An unconfigured/unrecognized model has NO entry —
`compute_cost()` returns `None`, never a guessed or zero cost.

To change a price: add a NEW dated entry under a new `rate_version`; never
edit an existing one in place. A usage row keeps the `rate_version` it was
actually computed under (the stage's "no retroactive backfill" rule) — an
old row's rate_version simply stops matching any CURRENT entry once
superseded, so nothing here can retroactively reprice it.

Sourced by direct, dated lookup this session (2026-09-01/09-02), not
scraped programmatically: Claude Sonnet 4.5
(anthropic.claude-sonnet-4-5-20250929-v1:0) and Claude Haiku 4.5
(anthropic.claude-haiku-4-5-20251001-v1:0) from Anthropic's own
direct-pricing page — Bedrock bills third-party models at the vendor's own
published rate, but these two were not independently re-confirmed against
AWS's own bedrock/pricing page (its per-model table did not render through
automated fetch). No entry exists for any legacy Claude model: a candidate
id/price pairing surfaced in search but could not be cleanly cross-confirmed
against AWS's current legacy-Sonnet figure before this stage's cutoff, and
adding it unconfirmed would be exactly the wrong-guess risk this module
exists to prevent — add it as a new dated entry once confirmed.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional, Tuple

_COST_QUANTIZE = Decimal("0.000001")  # matches bedrock_usage_events.cost_usd's NUMERIC(12, 6)


@dataclass(frozen=True)
class RateEntry:
    model_id: str
    rate_version: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal


def _entry(model_id: str, rate_version: str, input_per_million: str, output_per_million: str) -> RateEntry:
    """Fail-closed construction: a zero, negative, or malformed configured
    rate must never silently become a usable entry — better to crash at
    import time (loudly, in CI, before any usage row is ever priced) than to
    compute a cost of $0.00 or a negative charge for real usage."""
    try:
        input_d = Decimal(input_per_million)
        output_d = Decimal(output_per_million)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"malformed Bedrock rate for {model_id} ({rate_version}): {exc}") from exc
    if input_d <= 0 or output_d <= 0:
        raise ValueError(f"non-positive Bedrock rate for {model_id} ({rate_version})")
    return RateEntry(model_id=model_id, rate_version=rate_version,
                      input_usd_per_million=input_d, output_usd_per_million=output_d)


# The rate_version every entry below was added under. A future price change
# adds a NEW _entry(...) under a NEW version string in a NEW dict key set —
# see the module docstring's "never edit in place" rule.
CURRENT_RATE_VERSION = "2026-09-02"

RATES: Dict[str, RateEntry] = {
    entry.model_id: entry
    for entry in (
        _entry("anthropic.claude-sonnet-4-5-20250929-v1:0", CURRENT_RATE_VERSION, "3.00", "15.00"),
        _entry("anthropic.claude-haiku-4-5-20251001-v1:0", CURRENT_RATE_VERSION, "1.00", "5.00"),
    )
}


def rate_for(model_id: Optional[str]) -> Optional[RateEntry]:
    """The exact-match rate entry for `model_id`, or None if unconfigured."""
    if not model_id:
        return None
    return RATES.get(model_id)


def compute_cost(
    model_id: Optional[str], input_tokens: Optional[int], output_tokens: Optional[int]
) -> Optional[Tuple[Decimal, str]]:
    """`(cost_usd, rate_version)` for an EXACT model_id match with valid,
    non-negative token counts — else `None`. Never guesses: a None result
    means the caller must persist NULL cost/rate_version (the DB's own CHECK
    enforces they travel together) and count a `rate_unavailable` event,
    never fall back to $0.00 or an approximate rate.
    """
    entry = rate_for(model_id)
    if entry is None:
        return None
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None
    million = Decimal(1_000_000)
    cost = (
        (Decimal(input_tokens) * entry.input_usd_per_million / million)
        + (Decimal(output_tokens) * entry.output_usd_per_million / million)
    ).quantize(_COST_QUANTIZE)
    return cost, entry.rate_version
