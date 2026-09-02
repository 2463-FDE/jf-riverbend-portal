"""Intent-scoped tool selection and deterministic rendering of an eligibility
answer.

Two jobs, both taken away from the model on purpose:

1. WHICH TOOL RUNS. `classify_intent` maps the caller's own words to the one
   tool that can answer them — "is it active / verify" reaches only
   verify_current_eligibility, "what's on file / payer / member id" reaches
   only get_coverage_on_file, and both run only when the message explicitly
   asks for both. The classification narrows the tool list the model is
   offered AND is re-checked at dispatch (see raw_bedrock._dispatch_tool_call),
   so a model that ignores the offer still cannot reach the other tool. This
   is a narrowing of the existing server-bound tool set, never a widening:
   both tools remain bound to the authorized visit's own context and still
   take no model-supplied arguments (see eligibility_tool.py).

2. WHAT THE USER READS. Once a tool has actually run, its payload is rendered
   HERE, deterministically, and the model's own prose for that turn is
   discarded. A tool payload is a small, closed set of categorical outcomes
   (verified / simulated / unavailable / stored lookup); turning that into
   patient-facing text is a formatting job with a correct answer, not a
   generation job. Leaving it to the model is what produced markdown tables,
   invented next-step lists, and — the real hazard — prose that made a
   `simulated` or `unavailable` outcome read like a completed payer
   verification. Each outcome below maps to one fixed wording that says what
   actually happened.

The output contract: at most THREE short sentences, plain text, no markdown,
no emoji, no raw payload, no member id unless the caller expressly asked for
it (and then only the already-masked value the stored record holds).
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .eligibility_tool import COVERAGE_TOOL_NAME, COVERAGE_TOOL_SPEC, VERIFY_TOOL_NAME, VERIFY_TOOL_SPEC

MAX_SENTENCES = 3


class Intent(str, Enum):
    """What the caller asked for, in terms of the only two tools that exist."""

    VERIFY = "verify"
    COVERAGE = "coverage"
    BOTH = "both"
    # No recognisable eligibility ask (a greeting, an open question). The tool
    # set is left as it was — this module narrows intent, it never invents one.
    UNSPECIFIED = "unspecified"


# Substrings only, deliberately: this runs on the SCRUBBED message (see
# raw_bedrock), and a scrubbed message keeps its question words while its
# identifiers are already gone. Anything not matched stays UNSPECIFIED rather
# than being forced into a guess.
_VERIFY_SIGNALS = (
    "verify", "verified", "verification", "revalidat", "re-verif", "reverif",
    "check", "recheck", "re-check", "confirm", "eligib", "still active", "is it active",
    "is insurance active", "insurance valid", "is it valid", "coverage valid",
    # The way front-desk staff actually phrase a verification request. "covered"
    # carries the -ed deliberately: bare "cover" would also swallow questions
    # about what a plan pays for ("what does my plan cover for a physical?"),
    # which is neither tool's job. "coverage active" is likewise the whole
    # phrase, so it cannot fire on the stored-record wording "what coverage is
    # on file?".
    "covered", "coverage active", "coverage is active",
)
_COVERAGE_SIGNALS = (
    "on file", "on record", "stored", "member id", "member number", "member #",
    "payer", "insurer", "what plan", "which plan", "plan type", "what coverage",
    "which coverage", "coverage record",
)
# BOTH is only reachable when the message asks for the stored record AND a
# fresh check AND joins them — "what's on file and is it still active?".
# Without a conjunction, a message carrying both vocabularies (e.g. "check a
# different member id") is a single request, not two.
_CONJUNCTIONS = (" and ", " also ", " plus ", " as well", "both ", " then ")
_MEMBER_ID_SIGNALS = ("member id", "member number", "member #", "memberid")

# Structural markdown and symbol characters can only ever arrive here through
# a stored payer/plan string. Stripping them is what guarantees the frontend's
# plain-text render can never be handed a table row, a bullet, or an emoji —
# regardless of what a record happens to contain.
_MARKDOWN_CHARS = "|#*`_~[]{}<>"
_WHITESPACE = re.compile(r"\s+")
# A masked member id's own mask characters must survive that stripping: `*`
# is both a markdown emphasis character and the mask itself, and silently
# removing it would turn "****6789" into a bare "6789" — less masked, not
# more. `_MASK_CHARS` is also what proves a value IS masked before it is
# ever rendered (see _masked_member_id).
_MASK_CHARS = "*•xX"


@dataclass(frozen=True)
class IntentDecision:
    intent: Intent
    include_member_id: bool

    @property
    def tool_names(self) -> Tuple[str, ...]:
        if self.intent is Intent.VERIFY:
            return (VERIFY_TOOL_NAME,)
        if self.intent is Intent.COVERAGE:
            return (COVERAGE_TOOL_NAME,)
        return (VERIFY_TOOL_NAME, COVERAGE_TOOL_NAME)

    @property
    def tool_specs(self) -> list:
        specs = {VERIFY_TOOL_NAME: VERIFY_TOOL_SPEC, COVERAGE_TOOL_NAME: COVERAGE_TOOL_SPEC}
        return [specs[name] for name in self.tool_names]


def classify_intent(message: str) -> IntentDecision:
    text = (message or "").lower()
    wants_verify = any(signal in text for signal in _VERIFY_SIGNALS)
    wants_coverage = any(signal in text for signal in _COVERAGE_SIGNALS)
    include_member_id = any(signal in text for signal in _MEMBER_ID_SIGNALS)

    if wants_verify and wants_coverage:
        joined = any(conjunction in text for conjunction in _CONJUNCTIONS)
        # A joined request gets both; an unjoined one is a single verification
        # request that happens to name a record field.
        return IntentDecision(Intent.BOTH if joined else Intent.VERIFY, include_member_id)
    if wants_verify:
        return IntentDecision(Intent.VERIFY, include_member_id)
    if wants_coverage:
        return IntentDecision(Intent.COVERAGE, include_member_id)
    return IntentDecision(Intent.UNSPECIFIED, include_member_id)


def _clean(value, keep: str = "") -> str:
    """One stored field, reduced to plain text. Drops markdown structure and
    symbol/emoji codepoints while preserving ordinary letters (including
    accented ones) so a real payer name survives intact. `keep` exempts
    characters that carry meaning in a particular field — see _MASK_CHARS."""
    drop = str.maketrans({c: None for c in _MARKDOWN_CHARS if c not in keep})
    text = _WHITESPACE.sub(" ", str(value or "")).strip().translate(drop)
    return "".join(
        ch for ch in text if ch in keep or unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co")
    ).strip()


def _masked_member_id(value) -> Optional[str]:
    """The stored masked member id, or None if it does not actually look
    masked. "already safely masked" is checked here rather than assumed: a
    value carrying no mask character at all is withheld instead of printed,
    so a record that somehow held a full number cannot be surfaced just
    because the caller asked for the member id."""
    cleaned = _clean(value, keep=_MASK_CHARS)
    if not cleaned or not any(ch in _MASK_CHARS for ch in cleaned):
        return None
    return cleaned


def _friendly_date(as_of) -> Optional[str]:
    """`2026-08-23T14:00:00Z` -> `August 23, 2026`. Returns None for a missing
    or unparseable value so the sentence simply omits the date rather than
    printing a raw timestamp."""
    if not as_of:
        return None
    if isinstance(as_of, datetime):
        parsed = as_of
    else:
        try:
            parsed = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _status_word(status) -> str:
    cleaned = _clean(status).lower()
    return cleaned or "unknown"


def _verify_sentences(payload: dict) -> list:
    outcome = _clean(payload.get("outcome")).lower()
    status = _status_word(payload.get("status"))
    when = _friendly_date(payload.get("as_of"))

    if outcome == "simulated":
        # Never phrased as a completed check: the first sentence says the
        # check did not run, and the stored record is labelled as stored.
        return [
            "A current eligibility check was not run because this is a synthetic training environment.",
            f"Coverage on file is {status}.",
        ]
    if outcome == "unavailable":
        return [
            "Eligibility could not be verified right now.",
            f"The coverage record on file is {status}.",
            "Try again later or contact the payer.",
        ]
    # outcome == "verified": a real payer answer, so the status may be stated
    # as the current one — each status still gets its own honest wording.
    if status == "pending":
        return ["An eligibility check is in progress and has not returned a result yet."]
    if status == "stale":
        return [
            "Eligibility could not be re-checked just now, so this is the last known result."
            if not when
            else f"Eligibility could not be re-checked just now; the last known result is from {when}."
        ]
    if status == "unknown":
        return ["The payer did not return an eligibility result.", "Try again later or contact the payer."]
    return [f"Eligibility is {status} as of {when}." if when else f"Eligibility is {status}."]


def _coverage_sentences(payload: dict, include_member_id: bool) -> list:
    if not payload.get("has_coverage_on_file"):
        return ["No insurance coverage is on file for this visit."]

    payer = _clean(payload.get("payer_name"))
    plan = _clean(payload.get("plan_type"))
    status = _status_word(payload.get("status"))
    named = " ".join(part for part in (payer, plan) if part)

    sentences = []
    if named:
        sentences.append(f"Coverage on file is {named}.")
    sentences.append(f"Its stored status is {status}." if named else f"The stored coverage status is {status}.")
    if include_member_id:
        # Only ever the masked value the stored record already holds — this
        # module reads no other id field, and withholds even this one unless
        # it is genuinely masked.
        member_id = _masked_member_id(payload.get("member_id_masked"))
        if member_id:
            sentences.append(f"The member ID on file is {member_id}.")
    return sentences


def render_reply(
    *,
    verify_payload: Optional[dict] = None,
    coverage_payload: Optional[dict] = None,
    include_member_id: bool = False,
) -> Optional[str]:
    """The user-facing answer for one turn, or None when no tool ran (the
    caller then keeps whatever the model said, since there is no tool result
    to render). Verification leads when present — it is the current fact the
    caller asked about; stored coverage follows as context."""
    sentences = []
    if verify_payload is not None:
        sentences.extend(_verify_sentences(verify_payload))
    if coverage_payload is not None:
        sentences.extend(_coverage_sentences(coverage_payload, include_member_id))
    if not sentences:
        return None
    return " ".join(sentences[:MAX_SENTENCES])
