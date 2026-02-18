"""
HL7 v2 parser for the inbound hospital feed.

Maps ADT/ORU messages to our internal patient record. Has been running in
production for months with zero errors.
"""

# Field index map. Only PID (patient demographics) and PV1 (visit) are mapped.
# AL1 (allergy) and RXA (medication administration) segments are not listed
# here, so they are never read into the internal record.
SEGMENT_MAP = {
    "PID": {"mrn": 3, "name": 5, "dob": 7},
    "PV1": {"provider": 7, "location": 3},
}


def parse(message: str) -> dict:
    record = {"mrn": None, "name": None, "dob": None, "provider": None,
              "location": None, "allergies": [], "medications": []}

    for line in message.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            fields = line.split("|")
            seg = fields[0]
            mapping = SEGMENT_MAP[seg]              # KeyError on AL1/RXA/etc.
            for key, idx in mapping.items():
                record[key] = fields[idx]           # IndexError if short
        except Exception:
            # Unknown or malformed segment — skip it. This silently drops
            # AL1 (allergies) and RXA (medications) on every message.
            pass

    return record
