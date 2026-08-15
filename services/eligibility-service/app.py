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
import hmac
import asyncio
from typing import Optional

import redis as redis_lib
from fastapi import Depends, FastAPI, HTTPException, Header, Query
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


_MIN_INTERNAL_TOKEN_LENGTH = 32  # rejects "changeme" and any other short/example value


def _internal_token_is_configured() -> bool:
    """The same presence/length floor _verify_internal_token enforces per
    request, checked once at startup so a misconfigured deploy fails loudly
    instead of serving traffic that 401s every gateway-forwarded call. Mirrors
    intake-service and records-service."""
    configured = settings.internal_service_token
    return bool(configured) and len(configured) >= _MIN_INTERNAL_TOKEN_LENGTH


def _verify_internal_token(x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token")) -> None:
    """Prove this call came through the gateway.

    Cycle branch 7. This service published its port to the host and verified
    no caller at all, so anything able to reach the host could call it
    directly and bypass every permission check the gateway applies. The port
    is no longer published, but that is containment, not authentication — a
    caller already inside the compose network was still trusted blind.

    Mirrors services/intake-service/app.py::_verify_internal_token exactly:
    same shared INTERNAL_SERVICE_TOKEN, same fail-closed semantics. An
    unset/empty configured token, or a human-typed placeholder shorter than
    _MIN_INTERNAL_TOKEN_LENGTH, is never treated as "no check needed".

    This is transport trust — it proves the call arrived through the gateway.
    It is NOT per-resource authorization, and this branch does not claim to
    add that.
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

    The docstring on _internal_token_is_configured claimed this happened;
    nothing called it, so the function was dead code and this service would
    have started cleanly with a placeholder token and then rejected every
    request — the exact healthy-looking outage the round-13/17 reviews fixed
    for gateway, intake-service and records-service.

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


@app.get("/eligibility", response_model=EligibilityResponse, dependencies=[Depends(_verify_internal_token)])
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
@app.post("/eligibility/jobs", response_model=EligibilityJobResponse, status_code=201, dependencies=[Depends(_verify_internal_token)])
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


@app.get("/eligibility/jobs/{job_id}", response_model=EligibilityJobResponse, dependencies=[Depends(_verify_internal_token)])
def get_job(job_id: str):
    job = _job_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job)


@app.post("/eligibility/jobs/{job_id}/retry", response_model=EligibilityJobResponse, dependencies=[Depends(_verify_internal_token)])
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
@app.post("/visits/{visit_id}/messages", response_model=VisitMessageResponse, dependencies=[Depends(_verify_internal_token)])
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
