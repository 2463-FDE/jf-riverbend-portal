"""The structured draft the agent must produce, and the run result around it.

Numbers are carried as STRINGS and compared as `Decimal`: a computation claim
exists to be recomputed, and doing that in binary floating point would make
`7.5 - 6.2 == 1.3` false and refuse a correct draft. `extra="forbid"` throughout
— a model that invents a field is producing something this contract does not
describe, and dropping it silently would hide that.
"""
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from libs.agent_provenance import ProvenanceLabel

from .retrieval import RetrievalLedger


class QuoteClaim(BaseModel):
    """Words copied from a cited source. Quoting is a copy, not a generation."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["quote"]
    citation_id: str
    quote: str


class ComputationClaim(BaseModel):
    """Arithmetic over values that appear verbatim in the cited source."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["computation"]
    citation_id: str
    operator: Literal["subtract", "add"]
    operands: list
    result: str

    def recompute(self) -> Optional[Decimal]:
        try:
            values = [Decimal(str(o)) for o in self.operands]
        except (InvalidOperation, ValueError):
            return None
        if len(values) != 2:
            return None
        return values[0] - values[1] if self.operator == "subtract" else values[0] + values[1]

    def stated_result(self) -> Optional[Decimal]:
        try:
            return Decimal(str(self.result))
        except (InvalidOperation, ValueError):
            return None


Claim = Annotated[Union[QuoteClaim, ComputationClaim], Field(discriminator="kind")]


class StructuredDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1)
    claims: list = []

    def citation_ids(self) -> list:
        return list(dict.fromkeys(c.citation_id for c in self.claims))


# Bedrock returns the JSON inside a ```json fence. Stripping it is a transport
# detail, not a loosening of the contract: what is inside still has to satisfy
# the schema, the discriminated union and every validation rule unchanged.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class DraftParseError(ValueError):
    """The model returned something that is not a draft. Carries no model text."""


def parse_draft(payload: str) -> StructuredDraft:
    """Parse the agent's final message into a draft.

    Raises WITHOUT the offending text — that text is model output, which must
    not reach a log or a trace even on an error path. Claims go through a
    discriminated union, so an unknown `kind` is a parse failure rather than a
    silently dropped claim."""
    fenced = _FENCE.match(payload or "")
    if fenced:
        payload = fenced.group(1)
    try:
        raw = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise DraftParseError("final message was not JSON") from exc
    if not isinstance(raw, dict):
        raise DraftParseError("final message was not a JSON object")
    try:
        claims = TypeAdapter(list[Claim]).validate_python(raw.get("claims") or [])
        summary = raw.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise DraftParseError("draft had no summary text")
        return StructuredDraft(summary=summary, claims=claims)
    except ValidationError as exc:  # a pydantic message can quote the payload
        raise DraftParseError("a claim did not match its schema") from exc


@dataclass(frozen=True)
class UsageTurn:
    """One successful model round-trip's token usage — in memory only.
    W10 Final Stage 5 sub-slice 3: `provider`/`use_case` are the caller's
    (summary_agent_path.py) to attach at persistence time, not this
    library's; this carries only what the provider response itself
    reported. Never recorded for a failed call — no legitimate token count
    exists for one."""

    model_id: str
    turn: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


@dataclass
class AgentRunResult:
    """One run: the draft, where it came from, and the evidence behind it.

    `termination_reason` ("answered" | "max_turns" | "provider_error") is
    W10 Final Stage 4's truthful classification: a bounded LangGraph
    recursion-limit exception (GraphRecursionError) is loop exhaustion, not
    a provider outage, and must never share `provider_error`'s bucket even
    though both currently produce the same deterministic fallback draft.
    """

    draft: StructuredDraft
    label: ProvenanceLabel
    ledger: RetrievalLedger
    model_id: Optional[str] = None
    prompt_version: Optional[str] = None
    provider_error_type: Optional[str] = None
    termination_reason: Optional[str] = None
    citations: list = field(default_factory=list)
    usage: tuple = ()
