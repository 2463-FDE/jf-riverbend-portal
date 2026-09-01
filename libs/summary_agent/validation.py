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

from .contracts import MAX_SUMMARY_CHARACTERS, MAX_SUMMARY_SENTENCES, ComputationClaim, QuoteClaim, StructuredDraft
from .retrieval import RetrievalLedger

CODE_SUMMARY_TOO_LONG = "REFUSED_SUMMARY_TOO_LONG"
CODE_TOO_MANY_SENTENCES = "REFUSED_TOO_MANY_SENTENCES"
CODE_NO_CLAIMS = "REFUSED_NO_CLAIMS"
CODE_CITATION_NOT_RETRIEVED = "REFUSED_CITATION_NOT_RETRIEVED"
CODE_QUOTE_NOT_IN_SOURCE = "REFUSED_QUOTE_NOT_IN_SOURCE"
CODE_COMPUTATION_MISMATCH = "REFUSED_COMPUTATION_MISMATCH"
CODE_OPERAND_NOT_IN_SOURCE = "REFUSED_COMPUTATION_OPERAND_NOT_IN_SOURCE"
CODE_UNSUPPORTED_QUOTE_IN_SUMMARY = "REFUSED_UNSUPPORTED_QUOTE_IN_SUMMARY"
CODE_UNSUPPORTED_SUMMARY_SENTENCE = "REFUSED_UNSUPPORTED_SUMMARY_SENTENCE"
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
# A sentence ends at .!? followed by whitespace or end-of-string, optionally
# through a closing quote mark. Splitting on the punctuation alone would cut
# "1.3" in half and lose the very number a computation claim supports.
_SENTENCE_END = re.compile(r'(?<=[.!?])["”]?\s+')
_HAS_CONTENT = re.compile(r"[A-Za-z0-9]")
# A span that actually ENDS a sentence, rather than trailing off. The closing
# quote/bracket is optional because a sentence may legitimately finish inside
# one ('He said "no result."').
_SENTENCE_TERMINATED = re.compile(r"""[.!?]["”'’)\]]*$""")


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


def sentence_candidates(text: str) -> list:
    """EVERY sentence-shaped span in `text`, each an EXACT slice of it —
    including a final one that trails off with no terminal punctuation.

    One shared notion of a sentence BOUNDARY for the whole package (review
    finding SA-FALLBACK-SENTENCE-SCAN: the fallback used to have a second,
    narrower idea of its own). Slices are CUT from the source at
    `_SENTENCE_END` boundaries rather than reassembled from split fragments,
    so a candidate is always a verbatim, contiguous substring of `text` —
    exactly what a quote claim must be for `validate_draft` to accept it. The
    separator's own whitespace (and a closing quote mark sitting on the
    boundary) belongs to no sentence, the same way `_SENTENCE_END.split` has
    always dropped it.

    THE UNTERMINATED TAIL IS DELIBERATELY INCLUDED, and this function is the
    VALIDATOR's view for that reason: every scrap of a draft's summary has to
    be counted and grounded, so text trailing off without a full stop must
    still face the per-sentence check. Dropping it here would let
    `"<valid quote>" You are cured and may stop your medication` validate,
    because the unsupported half would no longer be a sentence anybody looked
    at. A GENERATOR choosing text to publish needs the opposite default —
    see `complete_sentences`.
    """
    spans, start = [], 0
    for boundary in _SENTENCE_END.finditer(text):
        spans.append(text[start:boundary.start()])
        start = boundary.end()
    spans.append(text[start:])

    sentences = []
    for span in spans:
        sentence = span.strip()
        if _HAS_CONTENT.search(sentence):  # skip punctuation-only leftovers
            sentences.append(sentence)
    return sentences


def complete_sentences(text: str) -> list:
    """Only the candidates that actually finish a sentence — the GENERATOR's
    view, for anything choosing source text to publish.

    Review finding SA-INCOMPLETE-FRAGMENT-ACCEPTED: `sentence_candidates`
    hands back an unterminated tail on purpose, and `deterministic_draft()`
    was publishing that tail as a quote claim. It validated, because a
    fragment IS a verbatim substring of its source — so a chunk ending
    "Take this medication with" became a patient-visible clinical instruction
    cut off mid-clause. Retrieval makes this the common case rather than an
    exotic one: `retrieve()` truncates each chunk to the character budget, so
    the last sentence a ledger holds is routinely cut mid-word.

    A fragment is never published, and it is never completed or trimmed into
    something publishable either — if a document has no whole sentence that
    fits, the fallback simply has nothing to offer from it.
    """
    return [s for s in sentence_candidates(text) if _SENTENCE_TERMINATED.search(s)]


def _computation_sentence(claim: ComputationClaim) -> str:
    """The one sentence a computation claim licenses, generated from the claim.

    A sentence merely CONTAINING the right number is not evidence of anything:
    "Your A1c fell 1.3 points" and "The difference between 7.5 and 6.2 is 1.3"
    share a number and not a meaning, and only the second is what the arithmetic
    established. The first reads as a clinical interpretation the source never
    made. So the claim generates its sentence and the draft has to match it,
    rather than the draft asserting freely and the claim loosely corroborating.
    """
    left, right = (str(o) for o in claim.operands)
    verb = "difference between" if claim.operator == "subtract" else "sum of"
    return f"The {verb} {left} and {right} is {claim.result}."


def _is_instruction_shaped(text: str) -> bool:
    return any(p.search(text) for p in _INSTRUCTION_PATTERNS)


def _content_sentence_count(summary: str) -> int:
    """How many sentences `summary` carries, read through `sentence_candidates`
    so the concise-format cap, the grounding check below and the fallback's own
    selection all agree on what a "sentence" is."""
    return len(sentence_candidates(summary))


def validate_draft(draft: StructuredDraft, ledger: RetrievalLedger) -> ValidationOutcome:
    """Refuse on the first failure, cheapest and most dangerous check first."""
    if len(draft.summary) > MAX_SUMMARY_CHARACTERS:
        return ValidationOutcome(False, CODE_SUMMARY_TOO_LONG)

    if _content_sentence_count(draft.summary) > MAX_SUMMARY_SENTENCES:
        return ValidationOutcome(False, CODE_TOO_MANY_SENTENCES)

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

    # ...and every SENTENCE must carry evidence, not only the ones in quotation
    # marks. An unquoted assertion is how an unsupported claim rides along in a
    # draft whose other claims are perfectly valid — the prompt already requires
    # "every statement backed by a claim", and this is that rule enforced rather
    # than requested. A quote-backed sentence has to contain its validated quote
    # (or be a fragment of one); a computation-backed sentence has to BE the
    # sentence its claim generates, exactly.
    templates = {_normalize(_computation_sentence(c)) for c in draft.claims
                 if isinstance(c, ComputationClaim)}
    for sentence in sentence_candidates(draft.summary):
        normalized = _normalize(sentence)
        if normalized in templates:
            continue
        if any(q and (q in normalized or normalized in q) for q in validated_quotes):
            continue
        return ValidationOutcome(False, CODE_UNSUPPORTED_SUMMARY_SENTENCE)

    return ValidationOutcome(True, None)
