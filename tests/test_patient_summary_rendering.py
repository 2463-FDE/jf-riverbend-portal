"""The patient summary's content rules, as pure logic (no DB, no HTTP).

These encode the client's settled rules of 2026-08-14. The table they agreed:

    single value  -> quote, and a change may be computed
    panel         -> quote, but NEVER a computed change
    prose         -> refuse; no quote at all

The rule that needs guarding hardest is the middle row. Withholding a panel
wholesale is the over-refusal the client called out by name, so there are
tests here asserting a panel still shows its numbers — not just that its delta
is absent.
"""
import pytest

from conftest import load_module

ps = load_module("services/records-service/patient_summary.py", "patient_summary_pure")

SINGLE = ps.ResultShape.SINGLE_VALUE
PANEL = ps.ResultShape.PANEL
UNQUOTABLE = ps.ResultShape.UNQUOTABLE


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["6.2%.", "2.3 mIU/L.", "140 mg/dL.", "98.6 F", "72 bpm."],
)
def test_a_single_measurement_is_quotable_and_may_carry_a_change(body):
    assert ps.classify(body, kind="lab_result") is SINGLE


@pytest.mark.parametrize(
    "body",
    [
        "WBC 6.1, RBC 4.7, Hgb 14.2.",
        "Na 140, K 4.1, Cr 0.9.",
        "Chol 190, HDL 55, LDL 110, Trig 120.",
    ],
)
def test_a_panel_is_quotable(body):
    """A panel is NOT withheld. The client ruled that out explicitly."""
    assert ps.classify(body, kind="lab_result") is PANEL


@pytest.mark.parametrize(
    "body",
    [
        "Patient reports intermittent headaches.",
        "Specimen hemolyzed; recollect.",
        "A1c 6.2%, discussed diet at length.",   # half structured, half prose
        "Within normal limits.",
        "",
        "   ",
        None,
    ],
)
def test_prose_takes_the_refusal_path(body):
    assert ps.classify(body, kind="lab_result") is UNQUOTABLE


@pytest.mark.parametrize(
    "body",
    [
        "Follow up in 3 months.",
        "Repeat in 6 weeks.",
        "RTC in 6 months.",          # appears verbatim in the seed
        "Recheck in 2 weeks.",
        "Call back after 5 days.",
        "Seen by Dr 5 times.",
        "Due for 2 vaccines.",
    ],
)
def test_a_scheduling_note_is_not_mistaken_for_a_measurement(body):
    """The false positive this parser is shaped to avoid.

    Each of these is a number with words in front of it, which a loose parser
    reads as a measurement — and would then quote to the patient as a result
    and subtract it from the last one. Deliberately classified here as a
    `lab_result`, so the prose-word rule is what has to reject them rather
    than the kind gate doing it for free.
    """
    assert ps.classify(body, kind="lab_result") is UNQUOTABLE


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Total chol 188, LDL 102, HDL 55.", PANEL),   # two-word analyte label
        ("Vitamin D 32 ng/mL.", SINGLE),
        ("Hgb A1c 6.2%.", SINGLE),
    ],
)
def test_a_two_word_analyte_label_is_not_over_refused(body, expected):
    """Found by running the parser over the real seed corpus.

    An earlier label rule capped the analyte name at a single six-character
    token. That rejected "Follow up in 3 months" correctly — and rejected
    "Total chol 188, LDL 102, HDL 55." with it, silently refusing every lipid
    panel in the seed. Refusing a real panel wholesale is the exact
    over-refusal the client ruled out, so the line is drawn by token count and
    vocabulary now, not by length alone.
    """
    assert ps.classify(body, kind="lab_result") is expected


def test_only_result_kinds_are_quoted_at_all():
    """A clinical note is prose by nature, whatever it happens to contain."""
    assert ps.classify("6.2%.", kind="note") is UNQUOTABLE
    assert ps.classify("6.2%.", kind="imaging_report") is UNQUOTABLE
    assert ps.classify("6.2%.", kind="lab_result") is SINGLE


# --- quoting is verbatim ---------------------------------------------------


def test_the_quote_is_the_stored_text_unchanged():
    """No re-casing, no unit normalising, no rounding. The guarantee the
    patient is given is that these are the report's own words."""
    for body in ["6.2%.", "WBC 6.1, RBC 4.7, Hgb 14.2.", "2.30 mIU/L."]:
        assert ps.quote_of(body) == body


def test_a_reference_range_is_rendered_verbatim_or_not_at_all():
    printed = "<5.7% normal; 5.7-6.4% prediabetes"
    assert ps.reference_range_of(printed) == printed


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_no_reference_range_means_no_reference_range(missing):
    """The branch that must not exist is the one that invents a category.

    If the report prints no range, the patient sees none — synthesizing
    "normal" from the value is precisely the judgment the client ruled out.
    """
    assert ps.reference_range_of(missing) is None


# --- computed change: the conservative half --------------------------------


def _m(label, value, unit):
    return ps.Measurement(label=label, value=value, unit=unit)


def test_a_change_between_two_single_values_is_arithmetic_not_judgment():
    change = ps.compute_change(_m("A1c", "6.2", "%"), _m("A1c", "5.8", "%"), prior_record_id=41)
    assert change is not None
    assert change.direction == "up"       # 5.8 -> 6.2 is a rise
    assert change.delta == "0.4"
    assert change.unit == "%"
    assert change.from_value == "5.8"
    assert change.from_record_id == 41    # traceable to the report it came from


def test_a_falling_value_reads_as_down():
    change = ps.compute_change(_m("A1c", "5.8", "%"), _m("A1c", "6.2", "%"), prior_record_id=41)
    assert change.direction == "down" and change.delta == "0.4"


def test_an_identical_value_is_unchanged_not_a_zero_delta_direction():
    change = ps.compute_change(_m("A1c", "6.2", "%"), _m("A1c", "6.2", "%"), prior_record_id=41)
    assert change.direction == "unchanged"


def test_the_delta_keeps_the_precision_the_report_used():
    """Float subtraction produces 1.2499999999999998 here. The patient must
    never see that."""
    change = ps.compute_change(
        _m("TSH", "2.30", "mIU/L"), _m("TSH", "1.05", "mIU/L"), prior_record_id=7
    )
    assert change.delta == "1.25"


def test_a_change_across_different_units_refuses():
    """Comparing mg/dL to mmol/L needs a conversion, and a conversion is a
    clinical claim this module will not make."""
    assert ps.compute_change(_m("X", "5", "mg/dL"), _m("X", "5", "mmol/L"), prior_record_id=1) is None


def test_a_change_on_an_unrecognised_unit_refuses():
    """"3 months" parses as a perfectly good measurement. Subtracting two
    follow-up intervals would present arithmetic on something that was never a
    lab value, so the unit allowlist stops it."""
    assert ps.compute_change(_m(None, "3", "months"), _m(None, "6", "months"), prior_record_id=1) is None


def test_a_change_with_no_unit_refuses():
    assert ps.compute_change(_m("K", "4.1", None), _m("K", "3.9", None), prior_record_id=1) is None


# --- the middle row of the table, stated directly --------------------------


def test_a_panel_quotes_its_numbers_even_though_its_delta_refuses():
    """Both halves of the panel rule in one assertion, because they are easy
    to conflate: the delta refusing must not take the quote down with it."""
    body = "WBC 6.1, RBC 4.7, Hgb 14.2."

    assert ps.classify(body, kind="lab_result") is PANEL
    assert ps.quote_of(body) == body                 # the numbers still show
    assert "6.1" in ps.quote_of(body)

    measurements = ps.parse_measurements(body)
    assert len(measurements) == 3                    # and it is genuinely multi-valued
    assert [m.label for m in measurements] == ["WBC", "RBC", "Hgb"]


def test_parsing_is_all_or_nothing_across_segments():
    """Quoting only the half that parsed would silently drop what the
    clinician wrote alongside it."""
    assert ps.parse_measurements("A1c 6.2%, discussed diet") is None


# --- the renderer that assembles items from chart rows ----------------------
#
# render_items is pure over rows — no database, no request, no response model —
# so it is exercised here with fakes. The DB- and HTTP-level behaviour is
# covered in tests/integration/test_patient_summary_flow.py.

from datetime import datetime, timezone  # noqa: E402


class _Row:
    def __init__(self, id, title, body, kind="lab_result", reference_range=None, day=1):
        self.id = id
        self.title = title
        self.body = body
        self.kind = kind
        self.reference_range = reference_range
        self.created_at = datetime(2026, 3, day, tzinfo=timezone.utc)


def _by_id(items):
    return {i.record_id: i for i in items}


def test_the_newest_result_is_shown_first():
    items = ps.render_items(
        [_Row(1, "A1c", "5.8%.", day=1), _Row(2, "A1c", "6.2%.", day=2)]
    )
    assert [i.record_id for i in items] == [2, 1]


def test_a_repeat_of_the_same_test_carries_a_change_back_to_the_earlier_one():
    items = _by_id(
        ps.render_items(
            [_Row(1, "A1c", "5.8%.", day=1), _Row(2, "A1c", "6.2%.", day=2)]
        )
    )

    assert items[2].change is not None
    assert items[2].change.direction == "up"
    assert items[2].change.delta == "0.4"
    assert items[2].change.from_record_id == 1        # links back to the source
    assert items[2].change.from_date == "2026-03-01"
    assert 1 in items[2].source_record_ids and 2 in items[2].source_record_ids

    # The first result has nothing to compare against and must not invent one.
    assert items[1].change is None


def test_a_change_is_never_computed_across_two_different_tests():
    """Comparing an A1c to a TSH is comparing different tests. Pairing is by
    title precisely so this cannot happen."""
    items = _by_id(
        ps.render_items(
            [_Row(1, "A1c", "5.8%.", day=1), _Row(2, "TSH", "2.3 mIU/L.", day=2)]
        )
    )
    assert items[2].change is None


def test_a_panel_shows_its_values_and_carries_no_change(  ):
    """The client's middle row, through the full renderer: repeated panels
    still quote every time, and never acquire a delta."""
    items = _by_id(
        ps.render_items(
            [
                _Row(1, "CBC panel", "WBC 6.0, RBC 4.6, Hgb 14.0.", day=1),
                _Row(2, "CBC panel", "WBC 6.1, RBC 4.7, Hgb 14.2.", day=2),
            ]
        )
    )

    assert items[2].quote == "WBC 6.1, RBC 4.7, Hgb 14.2."   # numbers still shown
    assert items[2].shape is PANEL
    assert items[2].change is None                            # but no delta
    assert items[2].refusal_reason is None                    # and NOT refused


def test_a_prose_result_refuses_and_shows_no_quote():
    items = _by_id(
        ps.render_items([_Row(1, "Note", "Specimen hemolyzed; recollect.")])
    )

    assert items[1].quote is None
    assert items[1].shape is UNQUOTABLE
    assert items[1].refusal_reason
    assert items[1].reference_range is None


def test_a_refused_result_never_also_carries_a_quote():
    """quote and refusal_reason are mutually exclusive by contract — a UI
    rendering both would show the very text the refusal withheld."""
    rows = [
        _Row(1, "A1c", "6.2%.", reference_range="<5.7% normal"),
        _Row(2, "Note", "Discussed results at length.", kind="note"),
        _Row(3, "CBC panel", "WBC 6.1, RBC 4.7."),
    ]
    for item in ps.render_items(rows):
        assert (item.quote is None) != (item.refusal_reason is None)


def test_the_reference_range_is_passed_through_verbatim():
    printed = "<5.7% normal; 5.7-6.4% prediabetes"
    items = _by_id(
        ps.render_items([_Row(1, "A1c", "6.2%.", reference_range=printed)])
    )
    assert items[1].reference_range == printed


def test_the_raw_body_is_never_echoed_as_a_field():
    """The response shape carries a quote, not a body. A client of this
    endpoint should not be able to render prose the rules said to withhold."""
    items = ps.render_items([_Row(1, "Note", "Prose here.", kind="note")])
    assert not hasattr(items[0], "body")
    assert "Prose here." not in repr(items[0])
