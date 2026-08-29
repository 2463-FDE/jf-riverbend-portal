"""
HL7 v2 parser for the inbound hospital feed.

Maps PID (demographics), PV1 (visit), AL1 (allergy), and RXA (medication
administration) segments into the internal patient record. Every segment in
a message is explicitly classified so a caller can tell "there was nothing
else to find" from "something was silently dropped":

  mapped                 — a known segment, extracted into `record`.
  incomplete_invalid      — a known segment present with too few fields to
                            extract its required data (truncated/malformed).
                            Never silently treated as absent.
  recognized_but_unmapped — a real HL7 v2 segment this parser does not
                            extract data from (an explicit, reviewed list —
                            not "anything we didn't map").
  ignored_standard        — a header/control segment; never clinical
                            content, always safe to skip.
  unknown                 — not a segment id this parser recognizes at all.

AL1/RXA were previously silently dropped (SEGMENT_MAP only listed PID/PV1) —
a known clinical-safety gap (RIV-160-adjacent, tests/README.md). Both are
now mapped; comprehension entries cover everything else so a caller can
tell what, if anything, was actually lost.
"""
from dataclasses import dataclass

# Field index map (0-indexed; fields[0] is always the segment id itself).
SEGMENT_MAP = {
    "PID": {"mrn": 3, "name": 5, "dob": 7},
    "PV1": {"provider": 7, "location": 3},
    "AL1": {"allergy": 3},
    "RXA": {"medication": 5},
}

# Header/control segments — never clinical content, always safe to skip.
IGNORED_STANDARD_SEGMENTS = {"MSH", "EVN", "BHS", "BTS", "FHS", "FTS"}

# Real HL7 v2 segments this parser recognizes but does not extract data
# from — reviewed and explicit, never inferred from "not in SEGMENT_MAP".
RECOGNIZED_UNMAPPED_SEGMENTS = {
    "NK1", "DG1", "OBX", "OBR", "IN1", "IN2", "GT1", "NTE", "ORC", "MRG", "ZDS",
}


@dataclass(frozen=True)
class SegmentComprehension:
    segment: str
    line_number: int
    status: str  # mapped | incomplete_invalid | recognized_but_unmapped | ignored_standard | unknown


@dataclass(frozen=True)
class ParseResult:
    record: dict
    segments: list


def parse(message: str) -> ParseResult:
    record = {"mrn": None, "name": None, "dob": None, "provider": None,
              "location": None, "allergies": [], "medications": []}
    segments = []

    for line_number, raw_line in enumerate(message.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split("|")
        seg = fields[0]

        if seg in SEGMENT_MAP:
            try:
                extracted = {key: fields[idx] for key, idx in SEGMENT_MAP[seg].items()}
            except IndexError:
                segments.append(SegmentComprehension(seg, line_number, "incomplete_invalid"))
                continue
            if seg == "AL1":
                record["allergies"].append(extracted["allergy"])
            elif seg == "RXA":
                record["medications"].append(extracted["medication"])
            else:
                record.update(extracted)
            segments.append(SegmentComprehension(seg, line_number, "mapped"))
        elif seg in IGNORED_STANDARD_SEGMENTS:
            segments.append(SegmentComprehension(seg, line_number, "ignored_standard"))
        elif seg in RECOGNIZED_UNMAPPED_SEGMENTS:
            segments.append(SegmentComprehension(seg, line_number, "recognized_but_unmapped"))
        else:
            segments.append(SegmentComprehension(seg, line_number, "unknown"))

    return ParseResult(record=record, segments=segments)
