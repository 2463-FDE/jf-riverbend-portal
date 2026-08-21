"""The 18 Safe Harbor identifier categories — 45 CFR 164.514(b)(2).

WHAT THIS IS, PRECISELY
=======================
A scrub that removes mechanically-detectable direct identifiers from text and
structured payloads before they reach a model or an analytics path.

WHAT IT IS NOT
==============
**It does not make data "de-identified" under Safe Harbor, and nothing built on
it may claim that.** Safe Harbor requires removal of all eighteen categories AND
that the covering entity has no actual knowledge the residual could identify
someone. Two categories cannot be met by pattern matching over narrative:

  * **(A) Names** — a regex cannot find "Maria" in prose without a list of names
    to look for. `scrub()` therefore accepts `known_identifiers`: the subject's
    own name parts, taken from the record being processed. That reliably removes
    the subject, and does NOT remove an incidentally-mentioned third party
    ("patient's daughter Ana drove her").
  * **(R) Any other unique identifying number, characteristic, or code** — open
    ended by definition. A rare diagnosis plus a small clinic is identifying,
    and no pattern detects that.

So the honest claim is: **direct identifiers removed, residual risk documented**.
Achieving Safe Harbor needs either verified removal of all eighteen (which
requires review of the residual) or an expert determination under
164.514(b)(1). That is a governance decision, not a code change — see the
recommendation gate.

DESIGN
======
Fail loud, not silent. `scrub()` returns a `DeidReport` alongside the text
saying which categories fired and how many times. It never reports the values
it removed: a "de-identification log" containing the identifiers would recreate
the exposure it exists to prevent.

Ordering matters and is deliberate. Longer, more specific patterns run before
shorter ones, so an SSN is not first eaten by the generic digit-run rule and
reported as an account number.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from libs.safe_logging.redact import SENSITIVE_FIELD_NAMES

MASK = "[REDACTED]"
DATE_MASK = "[DATE]"
AGE_MASK = "90+"

# The eighteen categories, by their regulatory letter. Present so a reviewer can
# check coverage against the rule rather than against this file's imagination.
IDENTIFIER_CATEGORIES = {
    "A": "names",
    "B": "geographic subdivisions smaller than a state",
    "C": "dates (except year) and all ages over 89",
    "D": "telephone numbers",
    "E": "fax numbers",
    "F": "email addresses",
    "G": "social security numbers",
    "H": "medical record numbers",
    "I": "health plan beneficiary numbers",
    "J": "account numbers",
    "K": "certificate/license numbers",
    "L": "vehicle identifiers and serial numbers",
    "M": "device identifiers and serial numbers",
    "N": "web URLs",
    "O": "IP addresses",
    "P": "biometric identifiers",
    "Q": "full-face photographs and comparable images",
    "R": "any other unique identifying number, characteristic, or code",
}

# Categories this scrub CANNOT close on its own. Surfaced as data, not prose,
# so the recommendation gate can enumerate them rather than restate them.
RESIDUAL_RISK_CATEGORIES = {
    "A": "third-party names in narrative are not detected without a name list",
    "B": "a place name in prose ('the Riverbend East clinic') is not a pattern",
    "P": "biometric data is not text; detection is out of scope here",
    "Q": "images are not text; detection is out of scope here",
    "R": "open-ended by definition — a rare condition plus a small clinic identifies",
}

# --- patterns, most specific first ----------------------------------------- #
# Each entry: (category letter, name, compiled pattern, replacement)
_PATTERNS = [
    ("G", "ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), MASK),
    ("F", "email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), MASK),
    ("N", "url", re.compile(r"\bhttps?://[^\s<>\"']+"), MASK),
    ("O", "ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), MASK),
    # Fax before phone: the label is the only thing distinguishing them.
    ("E", "fax", re.compile(r"\bfax[:\s#]*\+?[\d\-.() ]{7,}\d\b", re.I), MASK),
    ("D", "phone", re.compile(r"\b(?:\+1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"), MASK),
    ("H", "mrn", re.compile(r"\bMRN[:\s#]*[A-Z0-9-]{4,}\b", re.I), MASK),
    ("I", "health_plan_id", re.compile(r"\b(?:member|policy|subscriber)[:\s#]*[A-Z0-9-]{5,}\b", re.I), MASK),
    ("J", "account", re.compile(r"\b(?:acct|account)[:\s#]*[A-Z0-9-]{4,}\b", re.I), MASK),
    ("K", "license", re.compile(r"\b(?:license|licence|cert(?:ificate)?)[:\s#]*[A-Z0-9-]{4,}\b", re.I), MASK),
    ("L", "vehicle", re.compile(r"\b(?:VIN|plate)[:\s#]*[A-Z0-9-]{5,}\b", re.I), MASK),
    ("M", "device", re.compile(r"\b(?:device|serial)[:\s#]*[A-Z0-9-]{4,}\b", re.I), MASK),
    ("B", "zip", re.compile(r"\b\d{5}(?:-\d{4})?\b"), MASK),
    # Dates: ISO and common US forms. Year alone is permitted by Safe Harbor,
    # so a bare 4-digit year is deliberately NOT matched.
    ("C", "date_iso", re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), DATE_MASK),
    ("C", "date_us", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), DATE_MASK),
]

# Ages over 89 must be aggregated; 89 and below may stay.
_AGE = re.compile(r"\b(\d{2,3})[- ]?(?:years?[- ]old|y/?o)\b", re.I)


@dataclass
class DeidReport:
    """What fired, never what was removed."""
    counts: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def categories(self) -> set:
        return {cat for cat, _ in (k.split(":", 1) for k in self.counts)}

    def __bool__(self) -> bool:
        return self.total > 0

    def summary(self) -> str:
        if not self.counts:
            return "no direct identifiers detected"
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"removed {self.total}: {parts}"


def _bump(report: DeidReport, category: str, name: str, n: int) -> None:
    if n:
        key = f"{category}:{name}"
        report.counts[key] = report.counts.get(key, 0) + n


def _aggregate_ages(text: str, report: DeidReport) -> str:
    def repl(m):
        try:
            age = int(m.group(1))
        except ValueError:  # pragma: no cover - group is \d{2,3}
            return m.group(0)
        if age > 89:
            _bump(report, "C", "age_over_89", 1)
            return f"{AGE_MASK} years old"
        return m.group(0)

    return _AGE.sub(repl, text)


def scrub(text: str, known_identifiers: Iterable[str] = ()) -> tuple:
    """Return (scrubbed_text, DeidReport).

    `known_identifiers` are literal strings to remove — the subject's own name
    parts, taken from the record being processed. Pattern matching cannot find a
    name in prose; a list can. Case-insensitive, longest first so "Maria
    Gonzalez" is removed as one unit rather than leaving a fragment behind.
    """
    if not text:
        return text, DeidReport()

    report = DeidReport()
    out = text

    for ident in sorted({i.strip() for i in known_identifiers if i and i.strip()},
                        key=len, reverse=True):
        pattern = re.compile(re.escape(ident), re.I)
        out, n = pattern.subn(MASK, out)
        _bump(report, "A", "known_name", n)

    out = _aggregate_ages(out, report)

    for category, name, pattern, replacement in _PATTERNS:
        out, n = pattern.subn(replacement, out)
        _bump(report, category, name, n)

    return out, report


def scrub_structured(payload: Any, known_identifiers: Iterable[str] = ()) -> tuple:
    """Scrub a dict/list payload: sensitive KEYS are dropped entirely, and every
    remaining string value is passed through `scrub`.

    Key matching reuses `libs.safe_logging.redact.SENSITIVE_FIELD_NAMES` rather
    than keeping a second list — two lists of PHI field names drift, and the
    one that drifts is the one nobody is looking at.
    """
    report = DeidReport()

    def walk(node):
        if isinstance(node, Mapping):
            result = {}
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in SENSITIVE_FIELD_NAMES:
                    _bump(report, "A", f"field:{k.lower()}", 1)
                    result[k] = MASK
                else:
                    result[k] = walk(v)
            return result
        if isinstance(node, (list, tuple)):
            return [walk(v) for v in node]
        if isinstance(node, str):
            scrubbed, sub = scrub(node, known_identifiers)
            for key, n in sub.counts.items():
                cat, name = key.split(":", 1)
                _bump(report, cat, name, n)
            return scrubbed
        return node

    return walk(payload), report
