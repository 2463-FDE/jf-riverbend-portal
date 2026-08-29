"""
Tests for the inbound HL7 v2 parser (interop-service).

W10 Final Stage 2: AL1 (allergies) and RXA (medications) are now mapped —
the xfail this file used to carry (test_allergies_and_medications_are_
captured) is replaced by a real, passing assertion below, now that the
behavior it described is actually implemented. parse() also now returns an
explicit per-segment comprehension result (ParseResult.segments) instead of
a bare record dict, so a caller can tell "nothing else was there" from
"something was silently dropped" — see hl7_parser.py's module docstring.
"""
import os

from conftest import REPO_ROOT, load_module

hl7 = load_module("services/interop-service/hl7_parser.py", "interop_hl7_parser")

SAMPLE_PATH = os.path.join(REPO_ROOT, "services/interop-service/samples/adt_sample.hl7")
with open(SAMPLE_PATH) as fh:
    SAMPLE = fh.read()


def test_parses_patient_name_and_dob():
    result = hl7.parse(SAMPLE)
    assert result.record["name"] == "Gonzalez^Maria"
    assert result.record["dob"] == "19710302"


def test_parses_visit_provider_and_location():
    result = hl7.parse(SAMPLE)
    assert result.record["provider"] == "1234^Nguyen^Anita"
    assert result.record["location"] == "CLINIC^^^RIVERBEND"  # PV1 field index 3, as mapped today


def test_allergies_and_medications_are_captured():
    # The sample carries `AL1|...penicillin...` and `RXA|...amoxicillin...`.
    result = hl7.parse(SAMPLE)
    assert result.record["allergies"] == ["penicillin^Penicillin"]
    assert result.record["medications"] == ["amoxicillin^Amoxicillin 500mg"]


def test_every_sample_segment_is_mapped():
    result = hl7.parse(SAMPLE)
    statuses = {s.segment: s.status for s in result.segments}
    assert statuses["PID"] == "mapped"
    assert statuses["PV1"] == "mapped"
    assert statuses["AL1"] == "mapped"
    assert statuses["RXA"] == "mapped"


def test_msh_header_is_ignored_standard_not_unknown():
    result = hl7.parse(SAMPLE)
    msh = [s for s in result.segments if s.segment == "MSH"]
    assert msh and msh[0].status == "ignored_standard"


def test_a_recognized_but_unmapped_segment_is_labeled_as_such():
    result = hl7.parse(SAMPLE + "\nOBX|1|ST|glucose||120|mg/dL\n")
    obx = [s for s in result.segments if s.segment == "OBX"]
    assert obx and obx[0].status == "recognized_but_unmapped"


def test_unknown_segments_do_not_crash_and_are_labeled_unknown():
    result = hl7.parse(SAMPLE + "\nZZZ|garbage|line\n")
    assert result.record["name"] == "Gonzalez^Maria"  # unaffected
    zzz = [s for s in result.segments if s.segment == "ZZZ"]
    assert zzz and zzz[0].status == "unknown"


def test_a_truncated_mapped_segment_is_incomplete_invalid_not_silently_skipped():
    # PID normally needs field index 7 (dob) — this one stops at index 5.
    truncated = "MSH|^~\\&|A|B|C|D|20260601120000||ADT^A01|MSG1|P|2.3\nPID|1||M1^^^HOSP^MR||Doe^Jane\n"
    result = hl7.parse(truncated)
    pid = [s for s in result.segments if s.segment == "PID"]
    assert pid and pid[0].status == "incomplete_invalid"
    assert result.record["dob"] is None  # never invented, never silently "fine"


def test_line_numbers_are_1_indexed_and_track_the_original_message():
    result = hl7.parse(SAMPLE)
    pid = [s for s in result.segments if s.segment == "PID"][0]
    assert pid.line_number == 2  # MSH is line 1, PID is line 2 in the sample
