"""
eligibility-service — real-time payer eligibility (X12 270/271).

Front desk (and intake-service, inline) hit this before a visit to confirm a
member's coverage is active. The actual clearinghouse round-trip, plus the
Stage 1 resilience wrapper (bounded retries + circuit breaker + last-known-
good cache), lives in check.py.
"""
from fastapi import FastAPI, HTTPException, Query

from check import check
from config import settings
from contracts import EligibilityStatus
from logging_config import configure
from schemas import EligibilityResponse

log = configure(settings.service_name)
app = FastAPI(title="Riverbend eligibility-service", version="1.3.0")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.service_name}


@app.get("/eligibility", response_model=EligibilityResponse)
async def check_eligibility(insurance_id: str = Query(...)):
    insurance_id = (insurance_id or "").strip()
    if not insurance_id:
        raise HTTPException(status_code=422, detail="insurance_id must not be blank")

    result = await check(insurance_id)

    if result.error_type:
        # Never log insurance_id/member_id or a raw exception message here —
        # the error TYPE plus outcome status is enough for operational triage.
        log.warning(
            "eligibility check degraded (status=%s, error_type=%s)",
            result.status.value,
            result.error_type,
        )

    return EligibilityResponse(
        insurance_id=result.insurance_id,
        active=result.status == EligibilityStatus.ACTIVE,
        status=result.status,
        payer=settings.payer_name,
        raw_status=result.raw_status,
        checked_at=result.checked_at,
        stale=result.status == EligibilityStatus.STALE,
        error=result.error_type,
    )
