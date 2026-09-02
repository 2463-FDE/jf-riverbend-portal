"""libs/agent_budget — centrally enforced request bounds for the three
active paid Bedrock chat paths (W10 Metrics Stage 4): the worst-case-loop
cost formula, char-limit/cost-ceiling preflight rejection, and fail-closed
config validation. Per-surface "no provider call on preflight refusal" is
covered where each surface actually wires the check in
(tests/test_summary_agent_core.py, tests/test_policy_navigator_runtime.py,
tests/test_eligibility_agent_runtimes.py) — this file is the module's own
unit contract.
"""
from decimal import Decimal

import pytest

from libs.agent_budget import (
    BUDGETS,
    AgentBudget,
    BudgetExceededError,
    preflight_check,
    worst_case_cost,
    worst_case_tokens,
)
from libs.bedrock_pricing import RATES


def test_worst_case_tokens_accounts_for_growing_conversation_history():
    """Turn t's input carries turns 1..t-1's output too (Bedrock resends the
    whole running conversation every turn) — this is what makes the formula
    an actual worst case for the WHOLE loop, not just N * one_turn_cost."""
    budget = AgentBudget(use_case="x", max_input_chars=300, max_output_tokens=100, max_turns=3)

    total_input, total_output = worst_case_tokens(budget)

    i = 100  # ceil(300 / 3)
    # turn 1: I; turn 2: I + O; turn 3: I + 2O
    assert total_input == i + (i + 100) + (i + 200)
    assert total_output == 300  # 3 turns * 100


def test_worst_case_tokens_for_a_single_turn_is_just_the_input_and_output():
    budget = AgentBudget(use_case="x", max_input_chars=30, max_output_tokens=50, max_turns=1)

    total_input, total_output = worst_case_tokens(budget)

    assert total_input == 10  # ceil(30 / 3)
    assert total_output == 50


def test_zero_input_chars_is_a_valid_no_free_text_budget():
    """summary_agent_chat has no caller-supplied text at all — max_input_chars=0
    must not crash the token estimate."""
    budget = AgentBudget(use_case="x", max_input_chars=0, max_output_tokens=50, max_turns=2)

    total_input, total_output = worst_case_tokens(budget)

    assert total_input == 50  # only the growing-output contribution: 0+0, 0+50
    assert total_output == 100


def test_worst_case_cost_is_none_for_an_unpriced_model():
    assert worst_case_cost("policy_navigator_chat", "not-a-real-model") is None
    assert worst_case_cost("policy_navigator_chat", None) is None


def test_worst_case_cost_is_none_for_an_unknown_use_case():
    model_id = next(iter(RATES))
    assert worst_case_cost("not_a_real_use_case", model_id) is None


def test_worst_case_cost_is_positive_for_a_priced_model_and_a_real_use_case():
    model_id = next(iter(RATES))
    for use_case in BUDGETS:
        cost = worst_case_cost(use_case, model_id)
        assert cost is not None
        assert cost > 0


def test_preflight_check_allows_an_input_within_bounds():
    preflight_check("policy_navigator_chat", None, "a short question")  # must not raise


def test_preflight_check_rejects_an_oversized_input_regardless_of_model_pricing():
    budget = BUDGETS["policy_navigator_chat"]
    oversized = "x" * (budget.max_input_chars + 1)

    with pytest.raises(BudgetExceededError) as exc_info:
        preflight_check("policy_navigator_chat", None, oversized)
    assert exc_info.value.reason == "input_too_long"


def test_preflight_check_never_rejects_for_an_unpriced_model():
    """An unknown rate means the cost bound cannot be evaluated — that must
    never be treated as a rejection; only the char/turn bounds still apply."""
    preflight_check("policy_navigator_chat", "not-a-real-model", "a short question")


def test_preflight_check_rejects_when_the_worst_case_cost_exceeds_the_ceiling(monkeypatch):
    import libs.agent_budget as agent_budget

    monkeypatch.setattr(agent_budget, "MAX_WORST_CASE_COST_USD", Decimal("0.0000001"))
    model_id = next(iter(RATES))

    with pytest.raises(BudgetExceededError) as exc_info:
        preflight_check("policy_navigator_chat", model_id, "a short question")
    assert exc_info.value.reason == "worst_case_cost_exceeded"


def test_preflight_check_is_a_no_op_for_an_unknown_use_case():
    preflight_check("not_a_real_use_case", "any-model", "x" * 100_000)  # must not raise


@pytest.mark.parametrize("bad_value", ["0", "-1.00", "not-a-number"])
def test_a_zero_negative_or_malformed_budget_ceiling_is_rejected_at_load(monkeypatch, bad_value):
    """Exercises the SAME validation `MAX_WORST_CASE_COST_USD` is loaded
    through at import time, without reloading the module itself — a reload
    would mint a NEW `BudgetExceededError` class object, silently breaking
    every other module's already-imported `except BudgetExceededError:` for
    the rest of the process (they'd stop catching it)."""
    import libs.agent_budget as agent_budget

    monkeypatch.setenv("AGENT_MAX_WORST_CASE_COST_USD", bad_value)
    with pytest.raises(ValueError):
        agent_budget._load_max_worst_case_cost_usd()
