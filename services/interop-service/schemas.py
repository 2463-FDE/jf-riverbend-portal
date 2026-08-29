"""Pydantic v2 request/response schemas for interop-service."""
from typing import List, Optional

from pydantic import BaseModel, Field


class HL7IngestRequest(BaseModel):
    """Inbound HL7 v2 message. The gateway now POSTs JSON (not text/plain)."""

    message: str = Field(..., min_length=1, description="Raw HL7 v2 message text")


class ParsedRecord(BaseModel):
    """Internal record shape produced by hl7_parser.parse(). PID, PV1, AL1
    (allergies), and RXA (medications) are all mapped — see
    SegmentComprehensionEntry for what happened to every other segment."""

    mrn: Optional[str] = None
    name: Optional[str] = None
    dob: Optional[str] = None
    provider: Optional[str] = None
    location: Optional[str] = None
    allergies: List[str] = Field(default_factory=list)
    medications: List[str] = Field(default_factory=list)


class SegmentComprehensionEntry(BaseModel):
    """One line of the inbound message and what this parser did with it —
    see hl7_parser.py's module docstring for the five status values."""

    segment: str
    line_number: int
    status: str


class HL7IngestResponse(BaseModel):
    record: ParsedRecord
    segments: List[SegmentComprehensionEntry]
    # True when at least one segment was mapped-worthy (PID/PV1/AL1/RXA) but
    # too short to extract — required clinical content was present and
    # dropped, not merely absent. A caller must never read a 200 alone as
    # "nothing was lost."
    has_incomplete_content: bool
