"""Centrally enforced request bounds for the three active paid Bedrock chat
paths (summary_agent, policy_navigator, eligibility_agent) — W10 Metrics
Stage 4.

Before this module, each surface enforced its own bound ad hoc: policy_
navigator checked a question-length constant in services/records-service/
app.py, the eligibility visit-chat schema had its own max_length, and no
surface bounded output tokens or turns at all. BUDGETS below is now the one
place all three live.

These are config-time SAFETY CEILINGS, not per-user quotas: they reject a
request whose configured worst case would blow the training budget before
any provider egress, not track or throttle a specific patient/session. Not a
substitute for provider-side spend controls.

`preflight_check()` never raises for an unpriced model — its worst-case cost
is UNKNOWN (`None`), not a $0 pass or a rejection; only the char/turn bounds
still apply.
"""
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional, Tuple

from libs.bedrock_pricing import compute_cost

# Conservative token estimate from a raw character count, with NO tokenizer
# call: dividing by a SMALL chars-per-token constant intentionally
# OVER-estimates tokens (English prose averages ~4 chars/token; dense
# non-English text or numeric/ID-heavy text can run closer to 2). A safety
# ceiling must never UNDER-count — 3 chars/token stays conservative across
# realistic inputs without being absurdly pessimistic.
_CONSERVATIVE_CHARS_PER_TOKEN = 3


def _estimate_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return -(-chars // _CONSERVATIVE_CHARS_PER_TOKEN)  # ceiling division


@dataclass(frozen=True)
class AgentBudget:
    use_case: str
    max_input_chars: int
    max_output_tokens: int
    max_turns: int


# max_input_chars mirrors each surface's own existing bound where one already
# existed (policy_navigator's _POLICY_QUESTION_MAX=500, the eligibility visit
# message schema's max_length=2000) so this centralization changes no
# existing accepted-request behavior — it only adds the output-token/turn/
# cost dimensions those surfaces never bounded before. summary_agent has no
# free-text caller input at all (its "request" is a fixed instruction), so
# its char bound is 0 and the check is a no-op on that dimension for it.
BUDGETS: Dict[str, AgentBudget] = {
    "summary_agent_chat": AgentBudget(
        use_case="summary_agent_chat", max_input_chars=0, max_output_tokens=700, max_turns=4,
    ),
    "policy_navigator_chat": AgentBudget(
        use_case="policy_navigator_chat", max_input_chars=500, max_output_tokens=900, max_turns=4,
    ),
    "eligibility_agent_chat": AgentBudget(
        use_case="eligibility_agent_chat", max_input_chars=2000, max_output_tokens=700, max_turns=4,
    ),
}


def _load_max_worst_case_cost_usd() -> Decimal:
    """Fail closed the same way libs/bedrock_pricing's rate table does: a
    configured override that is zero, negative, or malformed must crash at
    import time, never silently become "no budget ceiling"."""
    raw = os.getenv("AGENT_MAX_WORST_CASE_COST_USD", "2.00")
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"malformed AGENT_MAX_WORST_CASE_COST_USD={raw!r}: {exc}") from exc
    if value <= 0:
        raise ValueError(f"non-positive AGENT_MAX_WORST_CASE_COST_USD={raw!r}")
    return value


# One shared training-budget ceiling across all three surfaces, not a
# per-use-case value: it answers "how much is this ONE request allowed to
# cost at the absolute worst case", regardless of which surface asks.
MAX_WORST_CASE_COST_USD = _load_max_worst_case_cost_usd()


class BudgetExceededError(Exception):
    """Raised by `preflight_check()`. `reason` is a small fixed vocabulary
    ("input_too_long" | "worst_case_cost_exceeded") — categorical only,
    never the input text or any provider/model detail, so it is always safe
    to fold directly into a termination_reason label or a log line."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def worst_case_tokens(budget: AgentBudget) -> Tuple[int, int]:
    """Total (input_tokens, output_tokens) across the ENTIRE possible agent
    loop for `budget`, not just one turn.

    Bedrock's Converse API resends the whole running conversation on every
    turn, so a bounded tool loop's input tokens do not stay flat across
    turns — turn t's input carries turns 1..t-1's output too. Modeling that
    growth (rather than pretending every turn costs like the first) is what
    makes this an actual worst case for "the entire possible agent loop", as
    the stage spec requires, not just `max_turns * one_turn_cost`:

        input_tokens(t)  = I + (t-1) * O      for t = 1..N
        output_tokens(t) = O                  for t = 1..N

    where I is the conservative input-token estimate for the initial
    request and O is the configured max_output_tokens. Summed over N turns:

        total_input  = N*I + O*N*(N-1)/2
        total_output = N*O
    """
    n = budget.max_turns
    i = _estimate_tokens(budget.max_input_chars)
    o = budget.max_output_tokens
    total_input = n * i + o * n * (n - 1) // 2
    total_output = n * o
    return total_input, total_output


def worst_case_cost(use_case: str, model_id: Optional[str]) -> Optional[Decimal]:
    """The worst-case USD cost of the entire possible loop for `use_case`
    under `model_id`'s versioned rate, or None if `use_case` is unknown or
    `model_id` has no configured rate (an unpriced model is not a $0 worst
    case — it is an UNKNOWN one)."""
    budget = BUDGETS.get(use_case)
    if budget is None:
        return None
    total_input, total_output = worst_case_tokens(budget)
    priced = compute_cost(model_id, total_input, total_output)
    return priced[0] if priced is not None else None


def preflight_check(use_case: str, model_id: Optional[str], input_text: str = "") -> None:
    """Raise BudgetExceededError before any provider egress if `input_text`
    or the configured worst-case loop cost would exceed this module's
    bounds. Returns None (allow) otherwise — including whenever the model's
    rate is unconfigured, since a cost bound cannot be evaluated without a
    price and this function must never block a request over a mere pricing
    gap."""
    budget = BUDGETS.get(use_case)
    if budget is None:
        return
    if budget.max_input_chars and len(input_text) > budget.max_input_chars:
        raise BudgetExceededError("input_too_long")
    cost = worst_case_cost(use_case, model_id)
    if cost is not None and cost > MAX_WORST_CASE_COST_USD:
        raise BudgetExceededError("worst_case_cost_exceeded")
