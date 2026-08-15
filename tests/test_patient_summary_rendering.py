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
# covered by the integration suite that ships with the routes, on the branch
# that adds them (this one deliberately contains no caller).

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


# --- review round 1 (#36/#37): analyte identity and chronology --------------


def test_a_change_between_two_different_analytes_refuses():
    """Adversarial review, #36: matching units and a shared title are not
    enough to make two results the same test.

    A feed that files "LDL 102 mg/dL" and "HDL 55 mg/dL" under one title would
    otherwise produce a delta between different analytes — same units,
    identical title, and a number the lab never reported.
    """
    ldl = _m("LDL", "102", "mg/dL")
    hdl = _m("HDL", "55", "mg/dL")
    assert ps.compute_change(ldl, hdl, prior_record_id=1) is None


def test_a_change_refuses_when_only_one_side_is_labelled():
    """Ambiguous identity is not an identity. If one result names its analyte
    and the other does not, there is no evidence they are the same test."""
    assert ps.compute_change(_m("LDL", "102", "mg/dL"), _m(None, "55", "mg/dL"), prior_record_id=1) is None
    assert ps.compute_change(_m(None, "102", "mg/dL"), _m("LDL", "55", "mg/dL"), prior_record_id=1) is None


def test_the_same_analyte_still_computes_across_spelling_variation():
    """Case and spacing vary between feeds; that must not block a real trend."""
    change = ps.compute_change(
        _m("Total chol", "188", "mg/dL"), _m("total  CHOL", "204", "mg/dL"), prior_record_id=9
    )
    assert change is not None and change.direction == "down" and change.delta == "16"


def test_two_unlabelled_results_of_one_test_still_compute():
    """The common case: "6.2%." twice under one title is the same test recorded
    twice, which is exactly what a change is for."""
    change = ps.compute_change(_m(None, "6.2", "%"), _m(None, "5.8", "%"), prior_record_id=3)
    assert change is not None and change.direction == "up"


def test_a_backfilled_record_does_not_invert_the_trend():
    """Adversarial review, #37: ids are not chronology.

    A lab imported late carries an older created_at and a LARGER id. Rows are
    now ordered by created_at, so the renderer must read the genuinely later
    result as the current one — ordering by id would show the backfilled row
    as newest and flip the arrow, telling a patient they were improving when
    their values got worse.
    """
    older_but_higher_id = _Row(99, "A1c", "5.8%.", day=1)   # backfilled later
    newer_but_lower_id = _Row(10, "A1c", "6.2%.", day=5)

    chronological = sorted([older_but_higher_id, newer_but_lower_id], key=lambda r: (r.created_at, r.id))
    items = _by_id(ps.render_items(chronological))

    # Newest first, by date rather than by id.
    assert [i.record_id for i in ps.render_items(chronological)] == [10, 99]

    # 5.8 -> 6.2 is a rise, and it is measured against the backfilled record.
    assert items[10].change is not None
    assert items[10].change.direction == "up"
    assert items[10].change.from_record_id == 99
    assert items[99].change is None


# --- review round 2 (#36): self-ordering and punctuation in analyte names ---


def test_render_items_orders_its_own_input_rather_than_trusting_the_caller():
    """Adversarial review, PS-ORDER-001.

    The chronological precondition used to be documented and unenforced, and
    a documented precondition is one nobody checks. Handed newest-first rows,
    render_items measured each result against the one that came AFTER it and
    reported the direction backwards — the probe returned "down 0.4" for a
    value that had risen. That is a patient-visible clinical claim, so the
    module orders its own input now instead of depending on the query being
    right.
    """
    newest_first = [_Row(2, "A1c", "6.2%.", day=2), _Row(1, "A1c", "5.8%.", day=1)]
    oldest_first = [_Row(1, "A1c", "5.8%.", day=1), _Row(2, "A1c", "6.2%.", day=2)]

    for rows in (newest_first, oldest_first):
        items = _by_id(ps.render_items(rows))
        assert items[2].change is not None
        assert items[2].change.direction == "up", "5.8 -> 6.2 is a rise in either input order"
        assert items[2].change.from_record_id == 1
        assert items[1].change is None


def test_ordering_falls_back_to_record_id_within_one_timestamp():
    """The seed writes whole charts inside a single now(), so ties are the
    normal case here — the order still has to be stable and reproducible."""
    rows = [_Row(3, "A1c", "6.2%.", day=1), _Row(1, "A1c", "5.8%.", day=1)]
    assert [i.record_id for i in ps.render_items(rows)] == [3, 1]


@pytest.mark.parametrize(
    "body,expected",
    [
        ("LDL-C 102 mg/dL.", SINGLE),            # hyphenated analyte
        ("25-OH Vitamin D 32 ng/mL.", SINGLE),   # leading digits, three tokens
        ("BP 120/80 mmHg.", SINGLE),             # paired vitals reading
        ("Total chol 188, LDL 102, HDL 55.", PANEL),
    ],
)
def test_punctuated_analyte_names_are_not_over_refused(body, expected):
    """Adversarial review, PS-PARSE-002 — the same over-refusal class the seed
    corpus caught for lipid panels, reached through punctuation instead of
    token count. Hyphens and slashes were doing what the old six-character cap
    used to do, and patients lost quotes the report plainly made."""
    assert ps.classify(body, kind="lab_result") is expected


@pytest.mark.parametrize(
    "body",
    [
        "Follow-up in 3 months.",   # hyphenated prose must not slip the word check
        "See patient in 2 weeks.",
        "Seen by Dr 5 times.",
    ],
)
def test_widening_the_label_rule_did_not_let_prose_through(body):
    """The risk in accepting punctuation: "Follow-up" could have hyphenated
    its way out of the prose-word list. Punctuation is stripped before that
    check precisely so it cannot."""
    assert ps.classify(body, kind="lab_result") is UNQUOTABLE


def test_a_paired_reading_quotes_but_never_computes_a_change():
    """Blood pressure is one result carrying two numbers. Subtracting them, or
    subtracting one pair from another, would state something no report made —
    and no special case is needed to prevent it, because a paired value simply
    is not a number."""
    current = ps.parse_measurements("BP 120/80 mmHg.")[0]
    prior = ps.parse_measurements("BP 118/76 mmHg.")[0]

    assert current.value == "120/80"
    assert current.as_float() is None
    assert ps.compute_change(current, prior, prior_record_id=1) is None
    assert ps.quote_of("BP 120/80 mmHg.") == "BP 120/80 mmHg."


def test_a_hyphenated_analyte_still_computes_a_real_trend():
    current = ps.parse_measurements("LDL-C 102 mg/dL.")[0]
    prior = ps.parse_measurements("LDL-C 118 mg/dL.")[0]

    change = ps.compute_change(current, prior, prior_record_id=4)
    assert change is not None
    assert (change.direction, change.delta, change.unit) == ("down", "16", "mg/dL")


@pytest.mark.parametrize(
    "body,expected_value,expected_unit",
    [
        ("6.2%.", "6.2", "%"),
        ("5.8%.", "5.8", "%"),
        ("2.3 mIU/L.", "2.3", "mIU/L"),
        ("102 mg/dL.", "102", "mg/dL"),
        ("LDL-C 102 mg/dL.", "102", "mg/dL"),
        ("25-OH Vitamin D 32 ng/mL.", "32", "ng/mL"),
        ("BP 120/80 mmHg.", "120/80", "mmHg"),
    ],
)
def test_the_parsed_number_is_the_whole_number(body, expected_value, expected_unit):
    """Pins the figure arithmetic actually runs on, not just the shape.

    Widening the label rule to accept digits and punctuation briefly let the
    label eat the start of the value: "6.2%" parsed as label "6." with value
    "2". Every shape assertion still passed and the quote stayed verbatim,
    because the quote is copied from the body — so nothing failed while a
    computed change would have used 2 instead of 6.2. Classification tests
    cannot catch that; only asserting the parsed value can.
    """
    measurements = ps.parse_measurements(body)
    assert measurements is not None, f"{body!r} should parse"
    assert len(measurements) == 1
    assert measurements[0].value == expected_value
    assert measurements[0].unit == expected_unit
