"""
Tests for the inbound HL7 v2 parser (interop-service).

These lock in the happy path the contractor cared about (patient demographics
+ visit). They do NOT prove allergies/medications survive the mapping — see the
xfail below, which documents the known RIV-160 / RIV-? data-loss gap.
"""
import os

from conftest import REPO_ROOT, load_module
import pytest

hl7 = load_module("services/interop-service/hl7_parser.py", "interop_hl7_parser")

SAMPLE_PATH = os.path.join(REPO_ROOT, "services/interop-service/samples/adt_sample.hl7")
with open(SAMPLE_PATH) as fh:
    SAMPLE = fh.read()


def test_parses_patient_name_and_dob():
    rec = hl7.parse(SAMPLE)
    assert rec["name"] == "Gonzalez^Maria"
    assert rec["dob"] == "19710302"


def test_parses_visit_provider_and_location():
    rec = hl7.parse(SAMPLE)
    assert rec["provider"] == "1234^Nguyen^Anita"
    assert rec["location"] == "CLINIC^^^RIVERBEND"  # PV1 field index 3, as mapped today


def test_unknown_segments_do_not_crash():
    # Malformed/extra segments must not raise — the parser swallows them.
    rec = hl7.parse(SAMPLE + "\nZZZ|garbage|line\n")
    assert rec["name"] == "Gonzalez^Maria"


def test_allergy_and_medication_segments_are_present_but_still_dropped_today():
    # Characterization, not the desired-behavior xfail below: SAMPLE genuinely
    # carries a recognized AL1 (penicillin) and RXA (amoxicillin) segment, not
    # garbage — this locks in that a well-formed but unmapped segment is
    # silently dropped exactly like a malformed one, per Week 6's comprehension
    # report (docs/planning/hl7-segment-comprehension-week6-08-25-2026.md).
    assert "AL1|" in SAMPLE and "RXA|" in SAMPLE
    rec = hl7.parse(SAMPLE)
    assert rec["allergies"] == []
    assert rec["medications"] == []


@pytest.mark.xfail(
    reason="AL1 (allergies) and RXA (medications) are silently dropped by the "
    "parser — SEGMENT_MAP only maps PID/PV1. Known clinical-safety gap, not yet "
    "fixed (the cohort fixes this).",
    strict=True,
)
def test_allergies_and_medications_are_captured():
    rec = hl7.parse(SAMPLE)
    # The sample carries `AL1|...penicillin...` and `RXA|...amoxicillin...`.
    assert rec["allergies"], "penicillin allergy should be parsed"
    assert rec["medications"], "amoxicillin medication should be parsed"
