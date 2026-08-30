"""Result contracts for the policy navigator (w-9-2-planner P3).

Stateless by design: no draft, no review gate, nothing persisted — the
navigator answers a question and returns, unlike the patient-summary agent's
versioned/reviewed draft path. `label` is one of
libs.agent_provenance.ProvenanceLabel's values (real/fixture/fallback); kept
as a plain string here so this module never needs that import either.
"""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class CitedSource:
    """One citation resolved back to display metadata — never just the raw
    id, so the UI never has to re-derive title/version/section itself."""

    citation_id: str
    source_id: str
    source_version: str
    title: str
    section_id: str


@dataclass(frozen=True)
class UsageTurn:
    """One successful model round-trip's token usage, in memory only —
    mirrors libs.summary_agent.contracts.UsageTurn (kept separate per
    ADR 0001: no shared lib between agent packages yet). Never recorded
    for a failed call — no legitimate token count exists for one."""

    model_id: str
    turn: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass(frozen=True)
class PolicyNavigatorResult:
    answer: str
    citations: Tuple[CitedSource, ...]
    label: str  # "real" | "fixture" | "fallback"
    model_id: Optional[str]
    termination_reason: str  # "answered" | "no_evidence" | "provider_error" | "citation_invalid" | "max_turns"
    usage: Tuple[UsageTurn, ...] = ()
