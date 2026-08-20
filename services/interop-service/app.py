"""
interop-service — ingests HL7 v2 messages from the hospital system feed.

The gateway now sends JSON ({"message": "<raw hl7>"}) rather than text/plain.
Parsing is delegated to hl7_parser.parse(), which is intentionally brittle: it
only maps PID/PV1 and silently drops AL1 (allergies) and RXA (medications).
That loss is preserved here on purpose (brittle-parser debt, D6).
"""
import hmac
import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException

from config import settings
from hl7_parser import parse
from logging_config import configure
from schemas import HL7IngestRequest, HL7IngestResponse, ParsedRecord

log = configure(settings.service_name)

app = FastAPI(title="Riverbend interop-service")

SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "samples", "adt_sample.hl7")

# Plain-language note returned with every ingest. We do NOT compute this from the
# message (the parser drops AL1/RXA before we ever see a segment count), so the
# loss stays invisible to callers — exactly the legacy behaviour.
UNMAPPED_NOTE = (
    "Only PID and PV1 segments are mapped into the internal record; "
    "other segments are not surfaced."
)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.service_name}


_MIN_INTERNAL_TOKEN_LENGTH = 32  # rejects "changeme" and any other short/example value


def _internal_token_is_configured() -> bool:
    """The same presence/length floor _verify_internal_token enforces per
    request, checked once at startup so a misconfigured deploy fails loudly
    instead of serving traffic that 401s every gateway-forwarded call. Mirrors
    intake-service, records-service, eligibility-service and
    scheduling-service."""
    configured = settings.internal_service_token
    return bool(configured) and len(configured) >= _MIN_INTERNAL_TOKEN_LENGTH


def _verify_internal_token(x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token")) -> None:
    """Prove this call came through the gateway.

    Cycle branch 7B. This service verified no caller at all, so anything able
    to reach it on the compose network could call it directly and bypass every
    permission check the gateway applies. #39 unpublished its host port, but
    that is containment, not authentication — a caller already inside the
    network was still trusted blind, with a forged X-Actor-Id if it liked.

    Mirrors services/eligibility-service/app.py::_verify_internal_token
    exactly: same shared INTERNAL_SERVICE_TOKEN, same fail-closed semantics.
    An unset/empty configured token, or a human-typed placeholder shorter than
    _MIN_INTERNAL_TOKEN_LENGTH, is never treated as "no check needed".

    This is transport trust — it proves the call arrived through the gateway.
    It is NOT per-resource authorization, and this branch does not claim to
    add that. See the PR body for what remains deferred.
    """
    configured = settings.internal_service_token
    if (
        not configured
        or len(configured) < _MIN_INTERNAL_TOKEN_LENGTH
        or not x_internal_token
        or not hmac.compare_digest(x_internal_token, configured)
    ):
        raise HTTPException(status_code=401, detail="missing or invalid internal service token")


@app.on_event("startup")
def _fail_fast_on_an_unusable_token() -> None:
    """Refuse to start rather than serve traffic that 401s everything.

    Compose's ${INTERNAL_SERVICE_TOKEN:?...} stops an entirely MISSING value
    before any container starts. It cannot catch a value that is present but
    unusable — "changeme", or anything under the length floor — which is
    precisely the case this check exists for.
    """
    if not _internal_token_is_configured():
        raise RuntimeError(
            f"INTERNAL_SERVICE_TOKEN is not set (or is shorter than "
            f"{_MIN_INTERNAL_TOKEN_LENGTH} chars) — refusing to start. Set a real "
            f"random value (e.g. `openssl rand -hex 32`) in .env; see .env.example."
        )


@app.post("/hl7/ingest", response_model=HL7IngestResponse, dependencies=[Depends(_verify_internal_token)])
def ingest(req: HL7IngestRequest):
    """Parse an inbound HL7 message into our internal record shape."""
    message = req.message
    if not message.strip():
        # Pydantic min_length catches empty strings; this catches whitespace-only.
        raise HTTPException(status_code=422, detail="message must not be empty")

    if len(message.encode("utf-8")) > settings.max_message_bytes:
        raise HTTPException(status_code=413, detail="message too large")

    try:
        record = parse(message)
    except Exception:
        # The parser swallows per-segment errors internally; this guards against
        # anything unexpected at the call boundary.
        log.exception("HL7 parse failed")
        raise HTTPException(status_code=422, detail="could not parse HL7 message")

    log.info("ingested HL7 message (%d bytes)", len(message.encode("utf-8")))
    # No schema validation of dropped/unmapped segments — AL1/RXA are already
    # gone by the time we get the record back.
    return HL7IngestResponse(
        record=ParsedRecord(**record), unmapped_note=UNMAPPED_NOTE
    )


@app.get("/hl7/sample", dependencies=[Depends(_verify_internal_token)])
def sample():
    """Return the bundled ADT sample message (useful for smoke-testing ingest)."""
    try:
        with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="sample message not found")
    return {"message": content}
