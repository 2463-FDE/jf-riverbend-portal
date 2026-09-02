"""libs/bedrock_pricing — the versioned Bedrock chat rate table and cost
computation (W10 Metrics Stage 4). No live vendor pricing is fetched here;
this only proves the module's own arithmetic and fail-closed behavior.
"""
from decimal import Decimal

import pytest

from libs.bedrock_pricing import RATES, _entry, compute_cost, rate_for


def test_exact_model_id_match_computes_the_correct_decimal_cost():
    model_id = next(iter(RATES))
    entry = RATES[model_id]

    result = compute_cost(model_id, 1_000_000, 1_000_000)

    assert result is not None
    cost, rate_version = result
    assert rate_version == entry.rate_version
    assert cost == entry.input_usd_per_million + entry.output_usd_per_million


def test_cost_scales_linearly_with_token_count():
    model_id = next(iter(RATES))
    entry = RATES[model_id]

    result = compute_cost(model_id, 500_000, 250_000)

    assert result is not None
    cost, _ = result
    expected = (Decimal(500_000) * entry.input_usd_per_million / Decimal(1_000_000)) + (
        Decimal(250_000) * entry.output_usd_per_million / Decimal(1_000_000)
    )
    assert cost == expected.quantize(Decimal("0.000001"))


def test_an_unrecognized_model_id_has_no_computable_cost():
    assert rate_for("not-a-real-model") is None
    assert compute_cost("not-a-real-model", 100, 100) is None


def test_a_none_model_id_has_no_computable_cost():
    assert rate_for(None) is None
    assert compute_cost(None, 100, 100) is None


def test_missing_or_non_integer_token_counts_yield_no_cost():
    model_id = next(iter(RATES))
    assert compute_cost(model_id, None, 100) is None
    assert compute_cost(model_id, 100, None) is None
    assert compute_cost(model_id, 1.5, 100) is None  # not an int


def test_negative_token_counts_yield_no_cost():
    model_id = next(iter(RATES))
    assert compute_cost(model_id, -1, 100) is None
    assert compute_cost(model_id, 100, -1) is None


def test_zero_tokens_computes_a_zero_but_defined_cost():
    model_id = next(iter(RATES))
    result = compute_cost(model_id, 0, 0)
    assert result == (Decimal("0.000000"), RATES[model_id].rate_version)


@pytest.mark.parametrize("bad_input, bad_output", [("0", "5.00"), ("5.00", "0"), ("-1.00", "5.00"), ("5.00", "-1.00")])
def test_a_zero_or_negative_configured_rate_is_rejected_at_construction(bad_input, bad_output):
    with pytest.raises(ValueError):
        _entry("some-model", "2099-01-01", bad_input, bad_output)


@pytest.mark.parametrize("bad_input, bad_output", [("not-a-number", "5.00"), ("3.00", "not-a-number")])
def test_a_malformed_configured_rate_is_rejected_at_construction(bad_input, bad_output):
    with pytest.raises(ValueError):
        _entry("some-model", "2099-01-01", bad_input, bad_output)


def test_the_currently_configured_rates_all_load_without_error():
    """The real module-level RATES table itself must never fail this same
    validation — if it did, the import would already have raised."""
    assert len(RATES) >= 2
    for entry in RATES.values():
        assert entry.input_usd_per_million > 0
        assert entry.output_usd_per_million > 0
