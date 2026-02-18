"""
interop-service — ingests HL7 v2 messages from the hospital system feed.
"""
from fastapi import FastAPI, Body

from hl7_parser import parse

app = FastAPI(title="Riverbend interop-service")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/hl7/ingest")
def ingest(message: str = Body(..., media_type="text/plain")):
    """Parse an inbound HL7 message into our internal record shape."""
    record = parse(message)
    # No schema validation, no count of dropped/unmapped segments.
    return {"record": record}
