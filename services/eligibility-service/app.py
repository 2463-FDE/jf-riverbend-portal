"""
eligibility-service — real-time payer eligibility (X12 270/271).

Front desk (and intake-service, via the Stage 3 async job path) hit this to
confirm a member's coverage. The actual clearinghouse round-trip, plus the
Stage 1 resilience wrapper (bounded retries + circuit breaker + last-known-
good cache), lives in check.py.

Stage 3 additions:
  * A Redis-backed eligibility job lifecycle (jobs.py) + in-process worker
    (worker.py), so intake-service can enqueue a check instead of blocking on
    it. The worker is started as a background asyncio task on app startup —
    see _start_worker below.
  * A visit-scoped chat endpoint wired to the Stage 2 AgentRuntime
    (agent_wiring.py), reusing this service's own resilient check() via the
    check_eligibility tool's HTTP call to /eligibility.
  * Metadata-only OpenTelemetry spans (libs/tracing) — correlation IDs and
    outcome/status attributes only, never a member ID, prompt, or payload.
"""
import asyncio
from typing import Optional

import redis as redis_lib
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from agent_wiring import bind_visit_context, handle_visit_message
from check import check
from config import settings
from contracts import EligibilityStatus
from jobs import EligibilityJob, JobStatus, JobStoreUnavailable, RedisEligibilityJobStore
from libs.tracing import new_correlation_id, safe_span
from logging_config import configure
from schemas import (
    CreateEligibilityJobRequest,
    EligibilityJobResponse,
    EligibilityResponse,
    VisitMessageRequest,
    VisitMessageResponse,
)
from worker import run_worker_loop

log = configure(settings.service_name)
app = FastAPI(title="Riverbend eligibility-service", version="1.4.0")

_TRACER_NAME = "eligibility-service"

_redis_client = None
_worker_task = None
_worker_stop_event = None


def _redis():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _job_store() -> RedisEligibilityJobStore:
    return RedisEligibilityJobStore(
        _redis(),
        max_retries=settings.job_max_retries,
        max_manual_retries=settings.job_max_manual_retries,
        status_ttl_seconds=settings.job_status_ttl_seconds,
        lease_seconds=settings.job_lease_seconds,
    )


@app.on_event("startup")
async def _start_worker():
    """Launch the in-process job-queue worker. It runs inside the SAME
    process as the API — this stack is one instance per clinic region
    (ARCHITECTURE.md; breaker.py's circuit-breaker state makes the same
    single-instance assumption) — so a container restart naturally restarts
    this task too. Any job left RUNNING when that happens is recovered by
    worker.py's own startup reclaim, never silently lost."""
    global _worker_task, _worker_stop_event
    _worker_stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(
        run_worker_loop(
            _job_store(),
            poll_interval_seconds=settings.worker_poll_interval_seconds,
            reclaim_interval_seconds=settings.worker_reclaim_interval_seconds,
            stop_event=_worker_stop_event,
        )
    )


@app.on_event("shutdown")
async def _stop_worker():
    if _worker_stop_event is not None:
        _worker_stop_event.set()
    if _worker_task is not None:
        _worker_task.cancel()


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": settings.service_name}


@app.get("/eligibility", response_model=EligibilityResponse)
async def check_eligibility(
    insurance_id: str = Query(...),
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    insurance_id = (insurance_id or "").strip()
    if not insurance_id:
        raise HTTPException(status_code=422, detail="insurance_id must not be blank")

    correlation_id = x_request_id or new_correlation_id()
    with safe_span(_TRACER_NAME, "eligibility.check", {"correlation_id": correlation_id}) as span:
        result = await check(insurance_id)
        span.set_attribute("status", result.status.value)
        if result.error_type:
            span.set_attribute("error.type", result.error_type)

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


# --------------------------------------------------------------------------- #
# Stage 3: async eligibility job lifecycle
# --------------------------------------------------------------------------- #
@app.post("/eligibility/jobs", response_model=EligibilityJobResponse, status_code=201)
def create_job(
    req: CreateEligibilityJobRequest,
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    insurance_id = (req.insurance_id or "").strip()
    if not insurance_id:
        raise HTTPException(status_code=422, detail="insurance_id must not be blank")

    correlation_id = x_request_id or new_correlation_id()
    # No idempotency_key from the caller means no idempotency protection for
    # THIS request (see CreateEligibilityJobRequest's docstring) — a fresh,
    # unique key is generated rather than silently reusing/guessing one.
    idempotency_key = req.idempotency_key or new_correlation_id()

    with safe_span(_TRACER_NAME, "eligibility.job.create", {"correlation_id": correlation_id}) as span:
        try:
            job = _job_store().create_or_reuse(insurance_id=insurance_id, idempotency_key=idempotency_key)
        except JobStoreUnavailable as exc:
            span.set_attribute("error.type", type(exc).__name__)
            log.warning("eligibility job enqueue unavailable (error_type=%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="eligibility job queue unavailable")
        span.set_attribute("job_status", job.status.value)

    return _job_response(job)


@app.get("/eligibility/jobs/{job_id}", response_model=EligibilityJobResponse)
def get_job(job_id: str):
    job = _job_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job)


@app.post("/eligibility/jobs/{job_id}/retry", response_model=EligibilityJobResponse)
def retry_job(job_id: str):
    job = _job_store().retry_manually(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.RETRYABLE:
        # Not eligible right now (still in flight, already succeeded, or
        # manual retries exhausted) — report the CURRENT state via 409
        # rather than raising a bare error.
        return JSONResponse(status_code=409, content=_job_response(job).model_dump(mode="json"))
    return _job_response(job)


def _job_response(job: EligibilityJob) -> EligibilityJobResponse:
    return EligibilityJobResponse(
        job_id=job.job_id,
        status=job.status,
        retry_count=job.retry_count,
        max_retries=job.max_retries,
        manual_retry_count=job.manual_retry_count,
        max_manual_retries=job.max_manual_retries,
        result_status=job.result_status,
        result_checked_at=job.result_checked_at,
        error=job.error_type,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


# --------------------------------------------------------------------------- #
# Stage 3: visit-scoped assistant turns
# --------------------------------------------------------------------------- #
@app.post("/visits/{visit_id}/messages", response_model=VisitMessageResponse)
def post_visit_message(
    visit_id: str,
    req: VisitMessageRequest,
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-Id"),
):
    correlation_id = x_request_id or new_correlation_id()
    bind_visit_context(visit_id, patient_id=req.patient_id, insurance_id=req.insurance_id)

    with safe_span(
        _TRACER_NAME,
        "eligibility.agent.turn",
        # Metadata only: correlation id + message LENGTH, never the message
        # text itself, a model reply, member ID, or tool payload.
        {"correlation_id": correlation_id, "message_length": len(req.message)},
    ) as span:
        result = handle_visit_message(visit_id, req.message)
        span.set_attribute("termination_reason", result.termination_reason.value)
        span.set_attribute("tool_called", result.tool_called)
        span.set_attribute("turns_used", result.turns_used)

    return VisitMessageResponse(
        visit_id=result.visit_id,
        reply=result.reply,
        tool_called=result.tool_called,
        eligibility_status=result.eligibility_status,
        termination_reason=result.termination_reason.value,
        turns_used=result.turns_used,
    )
