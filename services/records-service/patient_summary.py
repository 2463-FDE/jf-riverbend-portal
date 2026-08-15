"""Deterministic rendering of a patient's own results. No model, ever.

WHY THIS IS NOT PART OF THE AGENT
---------------------------------
`libs/patient_view_agent` is built on an invariant worth preserving: record
bodies never enter it. `contracts.RecordRow` deliberately omits `body` ("the
graph never needs the clinical narrative, so the read model does not carry
it"), `GraphNode.attributes` is documented as "minimum-necessary, never a
record body", and `patient_view_repository` never names `Record.body` in a
query. That is what makes the composer's model call safe: there is no clinical
narrative in scope for it to leak or paraphrase.

The client's content rules require the opposite for the patient's own view —
values quoted *verbatim* from `records.body`, with `reference_range` exactly as
the report printed it. Widening the agent to carry bodies would put clinical
narrative back within reach of a model, so this module reads them on a separate
path instead, and that path contains no model call at all. Quoting is a copy,
not a generation; nothing here needs a model, so nothing here has one.

THE THREE OUTCOMES (client, 2026-08-14)
---------------------------------------
| Stored result                     | Quote? | Compute a change? |
|-----------------------------------|--------|-------------------|
| Single value  ("6.2%.")           | yes    | yes               |
| Panel ("WBC 6.1, RBC 4.7, ...")   | yes    | NO                |
| Prose with no clean quote          | no     | no                |

A panel is never withheld wholesale — the client called that out as
over-refusing. Only the *computed change* refuses on a panel, because choosing
which analyte a "change" refers to is judgment, not arithmetic.

QUOTE GENEROUSLY, COMPUTE CONSERVATIVELY
----------------------------------------
These two operations carry different risk, so they have different bars.

Quoting is safe: the words are the report's own, shown with their date and a
link to the source record. Arithmetic is where a wrong answer becomes a claim
the report never made, so a change is only ever computed when both results are
single-valued, carry the *same* unit, and that unit is one this module
recognises. Anything else keeps the quote and drops the delta.

WE NEVER SYNTHESIZE A CATEGORIZATION
------------------------------------
`reference_range` is rendered exactly as stored or not at all. There is no
value-to-category mapping in this file and there must never be one: if the
report does not print the word "normal", the patient does not hear it from us.
"""
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResultShape(str, Enum):
    """What kind of quote a stored result can support."""

    SINGLE_VALUE = "single_value"   # quotable, and a change may be computed
    PANEL = "panel"                 # quotable; a change must NOT be computed
    UNQUOTABLE = "unquotable"       # no clean quote exists — refusal path


# A measurement segment: an optional analyte label, a number, an optional unit.
# The label is validated separately (see _label_is_analyte_like) rather than
# pinned down in this pattern, because the two things being told apart are not
# distinguishable by shape alone:
#
#     "Total chol 188"        -> a real lipid-panel analyte
#     "Follow up in 3 months" -> a scheduling note
#
# Both are words-then-number. An earlier version capped the label at a single
# six-character token, which rejected the second but ALSO rejected the first —
# and silently refused every seeded lipid panel, which is precisely the
# over-refusal the client ruled out. The label rule below draws the line by
# token count and vocabulary instead.
_MEASUREMENT = re.compile(
    r"""
    ^\s*
    (?P<label>[A-Za-z][A-Za-z0-9 ]*?)?
    \s*
    (?P<value>-?\d+(?:\.\d+)?)
    \s*
    (?P<unit>%|[A-Za-z][A-Za-z0-9]*(?:/[A-Za-z][A-Za-z0-9]*)?)?
    \s*$
    """,
    re.VERBOSE,
)

# Words that mark a phrase as prose rather than an analyte name. A label
# containing any of these is a sentence fragment ("Repeat in", "Follow up in"),
# not something a lab reports a number for.
_PROSE_WORDS = frozenset(
    {
        "in", "at", "to", "for", "after", "before", "within", "every", "next",
        "repeat", "follow", "up", "due", "recheck", "call", "see", "per",
        "and", "or", "with", "on", "by", "over", "about", "approx", "around",
    }
)

# Real analyte labels are one or two words ("Hgb", "Total chol", "Vitamin D").
# Three or more is a sentence.
_MAX_LABEL_TOKENS = 2
_MAX_LABEL_LENGTH = 16


def _label_is_analyte_like(label: Optional[str]) -> bool:
    """Whether a label reads as the name of something measured.

    No label at all is fine — "6.2%" is a perfectly good result. What is
    rejected is a label long enough, or worded enough, to be prose.
    """
    if not label:
        return True

    text = label.strip()
    if len(text) > _MAX_LABEL_LENGTH:
        return False

    tokens = text.split()
    if not tokens or len(tokens) > _MAX_LABEL_TOKENS:
        return False
    return not any(token.lower() in _PROSE_WORDS for token in tokens)

# Units a change may be computed across. An allowlist rather than "any unit"
# because the failure it prevents is specific: "3 months" parses as a perfectly
# good measurement, and subtracting two follow-up intervals would present
# arithmetic on something that was never a lab value. An unrecognised unit is
# not an error — the result still quotes, it just carries no delta.
_UNITS_SAFE_FOR_ARITHMETIC = frozenset(
    {
        "%",
        "mg/dL", "g/dL", "ng/mL", "pg/mL", "ug/dL", "mcg/dL",
        "mmol/L", "mEq/L", "umol/L", "mIU/L", "IU/L", "U/L", "mU/L",
        "K/uL", "M/uL", "cells/uL",
        "mmHg", "bpm", "kg", "lb", "cm", "mm",
        "mm/hr",
    }
)

# Only these record kinds are eligible for quoting at all. A clinical note is
# prose by nature; running a value parser over one invites exactly the
# false-positive this module is written to avoid.
_QUOTABLE_KINDS = frozenset({"lab_result", "vital_sign", "vitals"})


@dataclass(frozen=True)
class Measurement:
    """One parsed `label value unit` triple. Values stay strings until the
    moment arithmetic is actually authorised, so nothing is silently
    reformatted on the way to being displayed."""

    label: Optional[str]
    value: str
    unit: Optional[str]

    def as_float(self) -> Optional[float]:
        try:
            return float(self.value)
        except (TypeError, ValueError):
            return None


def _split_segments(body: str) -> list[str]:
    """Split a stored result into candidate measurement segments.

    The trailing period that every seeded body carries is punctuation, not
    content, so it is dropped before splitting — but only a *trailing* one, so
    a decimal point is untouched.
    """
    text = (body or "").strip()
    if text.endswith("."):
        text = text[:-1]
    return [seg for seg in (s.strip() for s in text.split(",")) if seg]


def parse_measurements(body: str) -> Optional[list[Measurement]]:
    """Every segment as a measurement, or None if any segment is not one.

    All-or-nothing on purpose. A body like "A1c 6.2%, discussed diet at length"
    is half structured and half prose; quoting only the half that parsed would
    silently drop clinical context the clinician wrote down. If any part of the
    result is prose, the whole result takes the refusal path.
    """
    segments = _split_segments(body)
    if not segments:
        return None

    parsed: list[Measurement] = []
    for segment in segments:
        match = _MEASUREMENT.match(segment)
        if match is None:
            return None

        label = (match.group("label") or "").strip() or None
        if not _label_is_analyte_like(label):
            return None

        parsed.append(
            Measurement(
                label=label,
                value=match.group("value"),
                unit=match.group("unit"),
            )
        )
    return parsed


def classify(body: str, *, kind: Optional[str] = None) -> ResultShape:
    """Which of the three outcomes this stored result supports."""
    if kind is not None and kind not in _QUOTABLE_KINDS:
        return ResultShape.UNQUOTABLE

    measurements = parse_measurements(body)
    if not measurements:
        return ResultShape.UNQUOTABLE
    return ResultShape.SINGLE_VALUE if len(measurements) == 1 else ResultShape.PANEL


def quote_of(body: str) -> str:
    """The result exactly as stored, whitespace-trimmed and nothing else.

    Deliberately not reformatted, re-cased, or unit-normalised: the guarantee
    the patient is given is that these are the report's own words.
    """
    return (body or "").strip()


def reference_range_of(reference_range: Optional[str]) -> Optional[str]:
    """The report's own range text, or nothing.

    There is no third branch here by design. When the report prints no range,
    the patient sees no range — a categorization we invented would be exactly
    the "normal/abnormal" judgment the client ruled out.
    """
    text = (reference_range or "").strip()
    return text or None


def _analyte_key(label: Optional[str]) -> str:
    """A comparable identity for what was measured.

    Case and spacing vary between feeds ("Total chol" / "total  chol"), so
    those are normalised away. An absent label is its own key: two unlabelled
    single values under one title ("6.2%." twice) are the same test recorded
    twice, which is exactly the case a change is for.
    """
    return " ".join((label or "").split()).lower()


@dataclass(frozen=True)
class Change:
    """A difference between two single-valued results of the same test."""

    direction: str          # "up" | "down" | "unchanged"
    delta: str              # magnitude, formatted from the stored precision
    unit: Optional[str]
    from_value: str
    from_record_id: int
    from_date: Optional[str]


def compute_change(
    current: Measurement,
    prior: Measurement,
    *,
    prior_record_id: int,
    prior_date: Optional[str] = None,
) -> Optional[Change]:
    """A change between two measurements, or None when one must not be shown.

    The prior row's id and date are arguments rather than something the caller
    patches in afterwards: a change the patient can't trace back to the report
    it came from is exactly what the "link to its source" rule forbids, and a
    field left to be filled in later is a field that eventually isn't.

    Returns None — never a guess — when the analytes differ, when the units
    differ, when either unit is outside the arithmetic allowlist, or when
    either value is not a number. Refusing here costs the patient a sentence;
    getting it wrong states a clinical fact the report never contained.
    """
    # Same analyte, or no arithmetic. Callers pair results by record *title*,
    # and a title is not an analyte: a feed that files "LDL 102 mg/dL" and
    # "HDL 55 mg/dL" under one shared title would otherwise produce a delta
    # between two different tests — matching units, identical title, and a
    # number the lab never reported. Labels are what actually identify the
    # measurement, so they have to agree before anything is subtracted.
    if _analyte_key(current.label) != _analyte_key(prior.label):
        return None

    if current.unit != prior.unit:
        return None
    if current.unit not in _UNITS_SAFE_FOR_ARITHMETIC:
        return None

    now, before = current.as_float(), prior.as_float()
    if now is None or before is None:
        return None

    difference = now - before
    if difference == 0:
        direction = "unchanged"
    else:
        direction = "up" if difference > 0 else "down"

    # Format the delta at whatever precision the stored values carried, so a
    # change between "6.2" and "5.8" reads "0.4" rather than "0.40000000000004".
    decimals = max(_decimals_in(current.value), _decimals_in(prior.value))
    magnitude = f"{abs(difference):.{decimals}f}"

    return Change(
        direction=direction,
        delta=magnitude,
        unit=current.unit,
        from_value=prior.value,
        from_record_id=prior_record_id,
        from_date=prior_date,
    )


def _decimals_in(value: str) -> int:
    _, _, fraction = (value or "").partition(".")
    return len(fraction)


@dataclass
class SummaryItem:
    """One result as the patient sees it.

    `quote` is None exactly when this item took the refusal path; the two are
    never both set, and `refusal_reason` is what the UI shows instead.
    """

    record_id: int
    title: Optional[str]
    date: Optional[str]
    shape: ResultShape
    quote: Optional[str] = None
    reference_range: Optional[str] = None
    change: Optional[Change] = None
    refusal_reason: Optional[str] = None
    # Every figure shown must be traceable to the row it came from — the
    # client's "a link to its source" requirement, carried in the payload
    # rather than reconstructed by the UI.
    source_record_ids: list[int] = field(default_factory=list)


REFUSAL_NO_CLEAN_QUOTE = (
    "This result is written as a note rather than a measurement, so it is shown "
    "to you only by your care team. Ask them about it at your next visit."
)


def render_items(rows) -> list[SummaryItem]:
    """Turn chart rows into quoted results, newest first.

    Lives here rather than in the route module because it is the content rules
    made concrete, and it needs no database, no request and no response model
    to be exercised — the route's job is to authorize and to serialize, not to
    decide what a patient may read.

    A change is only ever measured against the immediately preceding result of
    the *same title*. Comparing across titles would be comparing different
    tests; the rows arrive already scoped to one patient by the caller's
    authorization, and the title grouping is done here so the pairing rule is
    visible in one place.
    """
    previous_by_title: dict[str, tuple] = {}
    items: list[SummaryItem] = []

    for row in rows:
        shape = classify(row.body, kind=row.kind)
        date = row.created_at.date().isoformat() if row.created_at else None

        if shape is ResultShape.UNQUOTABLE:
            items.append(
                SummaryItem(
                    record_id=row.id,
                    title=row.title,
                    date=date,
                    shape=shape,
                    refusal_reason=REFUSAL_NO_CLEAN_QUOTE,
                    source_record_ids=[row.id],
                )
            )
            continue

        measurements = parse_measurements(row.body) or []
        change = None

        # Only a single-valued result can carry a change. On a panel the quote
        # still shows and the delta is dropped — choosing which analyte a
        # "change" refers to is judgment, not arithmetic.
        if shape is ResultShape.SINGLE_VALUE and row.title:
            prior = previous_by_title.get(row.title)
            if prior is not None:
                prior_row, prior_measurement = prior
                change = compute_change(
                    measurements[0],
                    prior_measurement,
                    prior_record_id=prior_row.id,
                    prior_date=(
                        prior_row.created_at.date().isoformat()
                        if prior_row.created_at
                        else None
                    ),
                )

        sources = [row.id]
        if change is not None:
            sources.append(change.from_record_id)

        items.append(
            SummaryItem(
                record_id=row.id,
                title=row.title,
                date=date,
                shape=shape,
                quote=quote_of(row.body),
                reference_range=reference_range_of(row.reference_range),
                change=change,
                source_record_ids=sources,
            )
        )

        if shape is ResultShape.SINGLE_VALUE and row.title:
            previous_by_title[row.title] = (row, measurements[0])

    # Newest first: the patient opens this to see the latest result, not the
    # oldest. Reversed AFTER the changes are computed, so the pairing above
    # still walks oldest -> newest. Callers must pass rows in chronological
    # order (see this module's note on ordering in get_patient_summary): a
    # change is "against the previous result", and that is only true if the
    # rows arrive in the order the results happened.
    items.reverse()
    return items
