"""Redis-backed eligibility job lifecycle (Stage 3 — RIV-088 / RIV-141).

Makes /intake's eligibility check asynchronous: intake-service enqueues a job
here instead of blocking on the payer round-trip, and the worker (worker.py)
drains the queue using this service's own resilient `check()` (Stage 1:
bounded retries, circuit breaker, last-known-good cache).

Namespacing — every key lives under its own prefix, distinct from every
other Redis use in this stack (gateway sessions: "session:{token}"; Stage 1's
last-known-good cache: "elig:lkg:{insurance_id}"; Stage 2's visit memory:
"agent:visit:{visit_id}"):
  * `elig:job:record:{job_id}`  — one job's full state (JSON string, see
    EligibilityJob below). Every write refreshes its TTL (status_ttl_seconds)
    so a job's state is available for polling but does not accumulate
    forever.
  * `elig:job:queue`            — a Redis LIST of job_ids waiting to be
    (re)claimed by a worker. FIFO via RPUSH/LPOP.
  * `elig:job:idem:{key}`       — idempotency_key -> job_id, same TTL as a
    job record, so a repeated create request within that window returns the
    SAME job (and never triggers a second live payer call) instead of
    silently creating a duplicate.
  * `elig:job:inflight`         — a Redis SET of job_ids currently RUNNING,
    used only for worker-restart recovery (see reclaim_expired below).

Job IDs are `uuid.uuid4().hex` — safe, opaque, non-guessable, and never
derived from a patient/member identifier (mirrors gateway session tokens in
services/gateway/security.py::create_session).

States — QUEUED -> RUNNING -> (SUCCEEDED | FAILED). A FAILED attempt is
immediately re-classified as RETRYABLE (re-enqueued, bounded by
max_retries) or DEAD_LETTER (retries exhausted). DEAD_LETTER can be moved
back to RETRYABLE exactly once (by default) via a controlled, explicit
manual retry (retry_manually), separately bounded by max_manual_retries so a
front-desk "try again" button can't retry forever either.

Minimized payload: EligibilityJob stores only what's needed to perform and
report a check — insurance_id (already handled unencrypted by check.py/
cache.py; no new exposure), status/lifecycle bookkeeping, and a terminal
result SUMMARY (status + checked_at + error TYPE). It never carries a
patient name/dob/ssn/notes, a raw payer response body, or any other PHI.

Worker-restart safety: because the queue, every job record, and the
in-flight set all live in Redis (not in the worker process's memory), a
container restart never silently loses a job. reclaim_expired() is the
recovery step a freshly-started worker runs before entering its normal loop
(and periodically thereafter): any RUNNING job whose lease has expired (the
previous worker died mid-processing without completing it) is requeued or
dead-lettered through the exact same bounded-retry path a live failure uses.

Best-effort reads mirror the Stage 1 Hardening Fix already applied to
cache.py/memory.py: a status *read* degrades to None on a Redis outage
rather than raising. Job *creation*, however, has no safe silent fallback —
losing an enqueue really does lose the request to check eligibility, so
create_or_reuse raises JobStoreUnavailable instead of pretending to
succeed; the caller (app.py) is responsible for turning that into a safe,
honest "unknown" response rather than a 500.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, List, Optional

from pydantic import BaseModel

log = logging.getLogger(__name__)

RECORD_PREFIX = "elig:job:record:"
QUEUE_KEY = "elig:job:queue"
IDEMPOTENCY_PREFIX = "elig:job:idem:"
INFLIGHT_KEY = "elig:job:inflight"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE = "retryable"
    DEAD_LETTER = "dead_letter"


# A job may be explicitly (re)tried from either of these — an ordinary
# automatic failure sitting in FAILED for an instant before this module
# reclassifies it, or a DEAD_LETTER a front-desk user asks to retry.
_MANUAL_RETRY_ELIGIBLE_STATUSES = frozenset({JobStatus.FAILED, JobStatus.DEAD_LETTER})


class EligibilityJob(BaseModel):
    job_id: str
    idempotency_key: str
    insurance_id: str
    status: JobStatus
    retry_count: int = 0
    max_retries: int
    manual_retry_count: int = 0
    max_manual_retries: int
    lease_expires_at: Optional[datetime] = None
    result_status: Optional[str] = None
    result_checked_at: Optional[datetime] = None
    error_type: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class JobStoreUnavailable(Exception):
    """Raised only when Redis is unreachable at job-CREATION time — there is
    no safe fallback for that (unlike a status read, which degrades to
    None). Callers must catch this and return a safe, honest degraded
    response, never let it surface as an unhandled 500."""


class RedisEligibilityJobStore:
    def __init__(
        self,
        redis_client,
        *,
        max_retries: int = 3,
        max_manual_retries: int = 1,
        status_ttl_seconds: int = 3600,
        lease_seconds: int = 30,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ):
        self._redis = redis_client
        self._max_retries = max_retries
        self._max_manual_retries = max_manual_retries
        self._status_ttl_seconds = status_ttl_seconds
        self._lease_seconds = lease_seconds
        self._now = now
        self._new_id = new_id

    # ---- creation / idempotency ------------------------------------------------

    def create_or_reuse(self, *, insurance_id: str, idempotency_key: str) -> EligibilityJob:
        """Returns the existing job for idempotency_key if one is still on
        record; otherwise creates, persists, and enqueues a new QUEUED job.
        Raises JobStoreUnavailable on any Redis failure.
        """
        try:
            existing_id = self._redis.get(self._idem_key(idempotency_key))
            if existing_id:
                existing = self._get_record(existing_id)
                if existing is not None:
                    return existing

            job = EligibilityJob(
                job_id=self._new_id(),
                idempotency_key=idempotency_key,
                insurance_id=insurance_id,
                status=JobStatus.QUEUED,
                max_retries=self._max_retries,
                max_manual_retries=self._max_manual_retries,
                created_at=self._now(),
                updated_at=self._now(),
            )
            self._put_record(job)
            self._redis.set(self._idem_key(idempotency_key), job.job_id, ex=self._status_ttl_seconds)
            self._redis.rpush(QUEUE_KEY, job.job_id)
            return job
        except Exception as exc:
            log.warning("eligibility job enqueue failed (error_type=%s)", type(exc).__name__)
            raise JobStoreUnavailable(type(exc).__name__) from exc

    def get(self, job_id: str) -> Optional[EligibilityJob]:
        try:
            return self._get_record(job_id)
        except Exception as exc:
            log.warning("eligibility job read failed (error_type=%s)", type(exc).__name__)
            return None

    # ---- worker-side lifecycle --------------------------------------------------

    def dequeue(self) -> Optional[EligibilityJob]:
        """Pop the next job_id and claim it (-> RUNNING, lease set). Returns
        None on an empty queue, a vanished record, or a Redis failure —
        never raises, so the worker loop can just try again next tick."""
        try:
            job_id = self._redis.lpop(QUEUE_KEY)
            if not job_id:
                return None
            job = self._get_record(job_id)
            if job is None:
                return None
            return self._mark_running(job)
        except Exception as exc:
            log.warning("eligibility job dequeue failed (error_type=%s)", type(exc).__name__)
            return None

    def _mark_running(self, job: EligibilityJob) -> Optional[EligibilityJob]:
        # Order matters: record this job as in-flight BEFORE writing the
        # RUNNING status. If the inflight-set write fails, the job's record
        # is untouched (still whatever it was — QUEUED/RETRYABLE) and it's
        # simply requeued; if we wrote RUNNING first and only then failed to
        # track it as in-flight, reclaim_expired's startup/periodic scan
        # (which only looks at the inflight set, not every record) could
        # never find it again — an untracked, permanently "running" job.
        # This does not close every conceivable interleaving (the inflight
        # add succeeding immediately followed by the status write itself
        # failing is still possible, and Redis single-node write ordering
        # makes the residual window small — the same best-effort, single-
        # instance posture breaker.py already takes for circuit-breaker
        # state), but it removes the far more likely failure mode of a
        # completely untracked, un-reclaimable job.
        try:
            self._redis.sadd(INFLIGHT_KEY, job.job_id)
        except Exception as exc:
            log.warning(
                "eligibility job inflight-set add failed, leaving job queued (error_type=%s)",
                type(exc).__name__,
            )
            self._redis.rpush(QUEUE_KEY, job.job_id)
            return None

        now = self._now()
        job = job.model_copy(
            update={
                "status": JobStatus.RUNNING,
                "lease_expires_at": now + timedelta(seconds=self._lease_seconds),
                "updated_at": now,
            }
        )
        self._put_record(job)
        return job

    def mark_succeeded(
        self, job: EligibilityJob, *, result_status: str, result_checked_at: Optional[datetime]
    ) -> None:
        job = job.model_copy(
            update={
                "status": JobStatus.SUCCEEDED,
                "result_status": result_status,
                "result_checked_at": result_checked_at,
                "error_type": None,
                "lease_expires_at": None,
                "updated_at": self._now(),
            }
        )
        self._put_record(job)
        self._safe_srem(job.job_id)

    def mark_failed_or_retry(self, job: EligibilityJob, *, error_type: Optional[str] = None) -> EligibilityJob:
        """A single attempt produced no usable result (check() degraded to
        `unknown`, or the worker hit an unexpected exception). Bumps
        retry_count; requeues (RETRYABLE) while under max_retries, else
        dead-letters. Either way the job record survives — nothing is
        dropped."""
        retry_count = job.retry_count + 1
        terminal = retry_count > job.max_retries
        job = job.model_copy(
            update={
                "status": JobStatus.DEAD_LETTER if terminal else JobStatus.RETRYABLE,
                "retry_count": retry_count,
                "error_type": error_type,
                "lease_expires_at": None,
                "updated_at": self._now(),
            }
        )
        self._put_record(job)
        self._safe_srem(job.job_id)
        if not terminal:
            self._redis.rpush(QUEUE_KEY, job.job_id)
        return job

    def retry_manually(self, job_id: str) -> Optional[EligibilityJob]:
        """Re-queue a FAILED/DEAD_LETTER job on explicit request, bounded by
        max_manual_retries. Returns None only if the job has no record at
        all; returns the job UNCHANGED (never raises) if it isn't currently
        in a retryable state or manual retries are exhausted — the caller
        (app.py) branches on the returned status to decide 200 vs 409."""
        job = self.get(job_id)
        if job is None:
            return None
        if job.status not in _MANUAL_RETRY_ELIGIBLE_STATUSES:
            return job
        if job.manual_retry_count >= job.max_manual_retries:
            return job
        job = job.model_copy(
            update={
                "status": JobStatus.RETRYABLE,
                "manual_retry_count": job.manual_retry_count + 1,
                "error_type": None,
                "updated_at": self._now(),
            }
        )
        self._put_record(job)
        self._redis.rpush(QUEUE_KEY, job.job_id)
        return job

    # ---- worker-restart recovery -------------------------------------------------

    def reclaim_expired(self) -> List[EligibilityJob]:
        """Find RUNNING jobs whose lease has expired — the worker that
        claimed them died mid-processing (crash, container restart) — and
        push them through the same bounded mark_failed_or_retry path a live
        failure uses. Call once on worker startup and periodically
        thereafter. Never raises: a Redis failure here just means recovery
        waits for the next tick."""
        reclaimed: List[EligibilityJob] = []
        try:
            job_ids = list(self._redis.smembers(INFLIGHT_KEY) or [])
        except Exception as exc:
            log.warning("eligibility job reclaim scan failed (error_type=%s)", type(exc).__name__)
            return reclaimed

        now = self._now()
        for job_id in job_ids:
            job = self.get(job_id)
            if job is None or job.status != JobStatus.RUNNING:
                self._safe_srem(job_id)
                continue
            if job.lease_expires_at is not None and job.lease_expires_at > now:
                continue  # still within its lease — a live worker owns it
            reclaimed.append(self.mark_failed_or_retry(job, error_type="WorkerLeaseExpired"))
        return reclaimed

    # ---- internals ---------------------------------------------------------------

    def _get_record(self, job_id: str) -> Optional[EligibilityJob]:
        raw = self._redis.get(self._record_key(job_id))
        if not raw:
            return None
        return EligibilityJob.model_validate_json(raw)

    def _put_record(self, job: EligibilityJob) -> None:
        self._redis.set(self._record_key(job.job_id), job.model_dump_json(), ex=self._status_ttl_seconds)

    def _safe_srem(self, job_id: str) -> None:
        try:
            self._redis.srem(INFLIGHT_KEY, job_id)
        except Exception as exc:
            log.warning("eligibility job inflight-set remove failed (error_type=%s)", type(exc).__name__)

    @staticmethod
    def _record_key(job_id: str) -> str:
        return f"{RECORD_PREFIX}{job_id}"

    @staticmethod
    def _idem_key(idempotency_key: str) -> str:
        return f"{IDEMPOTENCY_PREFIX}{idempotency_key}"
