"""Background worker draining the Redis-backed eligibility job queue
(jobs.py) — the async replacement for intake-service's old inline, unbounded
payer call (RIV-088 / RIV-141).

Runs in-process inside eligibility-service (started from app.py's FastAPI
startup event), reusing this service's OWN `check()` — the same Stage 1
resilient path (bounded retries, circuit breaker, last-known-good cache) the
synchronous /eligibility endpoint already uses, via the SAME module-level
breaker/cache singletons. There is deliberately no separate worker
container/process: this stack is one instance per clinic region
(ARCHITECTURE.md; breaker.py's own docstring makes the same call for the
circuit breaker), so an in-process asyncio task is the same scale mechanism
as the rest of Stage 1.

Worker-restart safety: `run_worker_loop` reclaims expired-lease jobs (see
jobs.py::reclaim_expired) once on startup AND periodically thereafter, so a
container restart — which kills this in-process task along with the rest of
the process — never silently loses a job. The queue and every job record
live in Redis, not in this task's memory.
"""
import asyncio
import logging
from typing import Callable, Optional

from check import check as _default_check
from jobs import EligibilityJob, RedisEligibilityJobStore

log = logging.getLogger(__name__)

# check() only ever returns ACTIVE/INACTIVE/STALE (a usable, terminal answer,
# even if STALE says so with a caveat) or UNKNOWN (no usable answer this
# attempt — retry-eligible). See services/eligibility-service/contracts.py.
_SUCCESS_STATUSES = frozenset({"active", "inactive", "stale"})


async def process_one(
    store: RedisEligibilityJobStore, *, check_fn: Callable = _default_check
) -> Optional[EligibilityJob]:
    """Claim and process exactly one job. Returns the completed job, or None
    if the queue was empty. Never raises: an unexpected failure from
    check_fn (which itself should not raise — Stage 1's check() always
    degrades gracefully — but a worker must not trust that blindly) is
    caught and routed through the same bounded retry-or-dead-letter path as
    an ordinary `unknown` result."""
    job = store.dequeue()
    if job is None:
        return None

    try:
        result = await check_fn(job.insurance_id)
    except Exception as exc:
        log.warning("eligibility worker check_fn raised (error_type=%s)", type(exc).__name__)
        return store.mark_failed_or_retry(job, error_type=type(exc).__name__)

    if result.status.value in _SUCCESS_STATUSES:
        store.mark_succeeded(job, result_status=result.status.value, result_checked_at=result.checked_at)
        return job
    return store.mark_failed_or_retry(job, error_type=result.error_type)


async def run_worker_loop(
    store: RedisEligibilityJobStore,
    *,
    check_fn: Callable = _default_check,
    poll_interval_seconds: float = 0.5,
    reclaim_interval_seconds: float = 15.0,
    stop_event: Optional[asyncio.Event] = None,
    sleep: Callable = asyncio.sleep,
    max_iterations: Optional[int] = None,
) -> None:
    """Drain the queue forever (or, in tests, for `max_iterations` ticks).

    `stop_event` lets app.py's shutdown handler end the loop cleanly;
    `max_iterations` lets tests run a bounded number of ticks without an
    external stop signal. Recovery (reclaim_expired) runs once immediately,
    then again every `reclaim_interval_seconds` of loop time.
    """
    store.reclaim_expired()
    elapsed_since_reclaim = 0.0
    iterations = 0

    while stop_event is None or not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            return
        iterations += 1

        job = await process_one(store, check_fn=check_fn)
        if job is None:
            await sleep(poll_interval_seconds)
            elapsed_since_reclaim += poll_interval_seconds

        if elapsed_since_reclaim >= reclaim_interval_seconds:
            store.reclaim_expired()
            elapsed_since_reclaim = 0.0
