"""Deterministic validation. No model, no judgement, no second opinion.

Every check is a mechanical comparison against evidence already in hand:
substring containment against the retrieved source, `Decimal` arithmetic,
membership in the ledger, a fixed regex list. Asking a model whether a model's
output was supported would be asking the faculty that produced the problem.
Refusal is terminal for a version, so each refusal returns a machine-readable
CODE — never a message, because "the quote 'X' is not in POL-001" would carry
the very text the trace boundary excludes.
"""
import re
from dataclasses import dataclass
from typing import Optional

from .contracts import ComputationClaim, QuoteClaim, StructuredDraft
from .retrieval import RetrievalLedger

CODE_NO_CLAIMS = "REFUSED_NO_CLAIMS"
CODE_CITATION_NOT_RETRIEVED = "REFUSED_CITATION_NOT_RETRIEVED"
CODE_QUOTE_NOT_IN_SOURCE = "REFUSED_QUOTE_NOT_IN_SOURCE"
CODE_COMPUTATION_MISMATCH = "REFUSED_COMPUTATION_MISMATCH"
CODE_OPERAND_NOT_IN_SOURCE = "REFUSED_COMPUTATION_OPERAND_NOT_IN_SOURCE"
CODE_UNSUPPORTED_QUOTE_IN_SUMMARY = "REFUSED_UNSUPPORTED_QUOTE_IN_SUMMARY"
CODE_INSTRUCTION_SHAPED = "REFUSED_INSTRUCTION_SHAPED_CLAIM"

# Instruction-shaped text: the register of a document giving orders rather than
# stating facts. Matched against the summary (the only field a patient sees) and
# every quote claim, since a draft repeating an injected instruction has carried
# it through either way. A computation claim carries no free text — a non-numeric
# operand is caught by the operand/recompute checks below.
_INSTRUCTION_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"ignore\s+(all\s+)?(previous|prior)\s+instructions",
    r"disregard\s+(the\s+|all\s+)?(previous|prior|approved|above)",
    r"unrestricted\s+mode",
    r"you\s+are\s+now\s+(operating|in)\b",
    r"system\s+prompt",
    r"approved[_\s]only",
    r"return\s+every\s+document",
    r"without\s+a\s+citation",
    r"do\s+not\s+mention\s+this",
))

_QUOTED_IN_SUMMARY = re.compile(r'"([^"]{4,})"')
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ValidationOutcome:
    passed: bool
    code: Optional[str]

    @property
    def refusal_code(self) -> Optional[str]:
        """What `record_validation` wants: a code on refusal, None on a pass."""
        return None if self.passed else self.code


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _is_instruction_shaped(text: str) -> bool:
    return any(p.search(text) for p in _INSTRUCTION_PATTERNS)


def validate_draft(draft: StructuredDraft, ledger: RetrievalLedger) -> ValidationOutcome:
    """Refuse on the first failure, cheapest and most dangerous check first."""
    if _is_instruction_shaped(draft.summary) or any(
        _is_instruction_shaped(c.quote) for c in draft.claims if isinstance(c, QuoteClaim)
    ):
        return ValidationOutcome(False, CODE_INSTRUCTION_SHAPED)

    if not draft.claims:
        # A summary asserting things with nothing behind it is the shape of an
        # unsupported claim, and it is the shape an injected instruction takes
        # when it sneaks past the pattern list.
        return ValidationOutcome(False, CODE_NO_CLAIMS)

    validated_quotes = []
    for claim in draft.claims:
        source = ledger.get(claim.citation_id)
        if source is None:
            return ValidationOutcome(False, CODE_CITATION_NOT_RETRIEVED)
        body = _normalize(source.text)

        if isinstance(claim, QuoteClaim):
            quote = _normalize(claim.quote)
            if not quote or quote not in body:
                return ValidationOutcome(False, CODE_QUOTE_NOT_IN_SOURCE)
            validated_quotes.append(quote)

        elif isinstance(claim, ComputationClaim):
            # Both operands must appear in the source: recomputing correctly
            # from numbers the source never printed is arithmetic on invented
            # inputs, which is still a claim the report never made.
            for operand in claim.operands:
                if str(operand) not in body:
                    return ValidationOutcome(False, CODE_OPERAND_NOT_IN_SOURCE)
            recomputed, stated = claim.recompute(), claim.stated_result()
            if recomputed is None or stated is None or recomputed != stated:
                return ValidationOutcome(False, CODE_COMPUTATION_MISMATCH)

    # Anything the summary presents in quotation marks must be one of the quote
    # claims that just passed — otherwise the draft shows the patient words it
    # never offered evidence for.
    for quoted in _QUOTED_IN_SUMMARY.findall(draft.summary):
        normalized = _normalize(quoted)
        if not any(normalized in q or q in normalized for q in validated_quotes):
            return ValidationOutcome(False, CODE_UNSUPPORTED_QUOTE_IN_SUMMARY)

    return ValidationOutcome(True, None)
