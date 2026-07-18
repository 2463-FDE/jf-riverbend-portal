"""Unit tests for the in-process eligibility job worker
(services/eligibility-service/worker.py).

Uses the SAME in-memory Redis double style as test_eligibility_jobs.py, and a
scripted fake check() so these tests never touch the network, a real payer,
or a real clock. Async entry points are driven via asyncio.run(), mirroring
tests/test_eligibility_check.py — no pytest-asyncio plugin is installed
(deliberately not in requirements-dev.txt), matching the rest of this repo's
async test style.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from conftest import load_module

jobs_mod = load_module("services/eligibility-service/jobs.py", "eligibility_jobs_for_worker")
worker_mod = load_module("services/eligibility-service/worker.py", "eligibility_worker")

RedisEligibilityJobStore = jobs_mod.RedisEligibilityJobStore
JobStatus = jobs_mod.JobStatus
QUEUE_KEY = jobs_mod.QUEUE_KEY
INFLIGHT_KEY = jobs_mod.INFLIGHT_KEY
process_one = worker_mod.process_one
run_worker_loop = worker_mod.run_worker_loop

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


class _FakeRedis:
    def __init__(self):
        self.strings = {}
        self.lists = {}
        self.sets = {}

    def get(self, key):
        return self.strings.get(key)

    def set(self, key, value, ex=None):
        self.strings[key] = value

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lpop(self, key):
        lst = self.lists.get(key)
        return lst.pop(0) if lst else None

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    def smembers(self, key):
        return set(self.sets.get(key, set()))


class _FakeStatus:
    def __init__(self, value):
        self.value = value


class _FakeResult:
    def __init__(self, status, checked_at=None, error_type=None):
        self.status = _FakeStatus(status)
        self.checked_at = checked_at
        self.error_type = error_type


def _store(redis=None, **kwargs):
    kwargs.setdefault("now", lambda: NOW)
    return RedisEligibilityJobStore(redis or _FakeRedis(), **kwargs)


# --- process_one ----------------------------------------------------------------


def test_process_one_on_empty_queue_returns_none():
    store = _store()

    result = asyncio.run(process_one(store, check_fn=None))

    assert result is None


def test_process_one_marks_a_successful_check_succeeded():
    store = _store()
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    async def fake_check(insurance_id):
        assert insurance_id == "MEM1"
        return _FakeResult("active", checked_at=NOW)

    job = asyncio.run(process_one(store, check_fn=fake_check))

    assert job.job_id == created.job_id
    stored = store.get(created.job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result_status == "active"


def test_process_one_treats_stale_as_a_success_not_a_retry():
    store = _store()
    store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    async def fake_check(insurance_id):
        return _FakeResult("stale", checked_at=NOW)

    job = asyncio.run(process_one(store, check_fn=fake_check))

    assert store.get(job.job_id).status == JobStatus.SUCCEEDED


def test_process_one_marks_unknown_result_retryable():
    redis = _FakeRedis()
    store = _store(redis, max_retries=3)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    async def fake_check(insurance_id):
        return _FakeResult("unknown", error_type="CircuitOpenError")

    asyncio.run(process_one(store, check_fn=fake_check))

    stored = store.get(created.job_id)
    assert stored.status == JobStatus.RETRYABLE
    assert stored.error_type == "CircuitOpenError"
    assert redis.lists[QUEUE_KEY] == [created.job_id]  # requeued, not dropped


def test_process_one_never_raises_even_if_check_fn_blows_up():
    store = _store(max_retries=2)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    async def exploding_check(insurance_id):
        raise RuntimeError("unexpected bug")

    asyncio.run(process_one(store, check_fn=exploding_check))  # must not raise

    stored = store.get(created.job_id)
    assert stored.status == JobStatus.RETRYABLE
    assert stored.error_type == "RuntimeError"
    assert "unexpected bug" not in (stored.error_type or "")


# --- run_worker_loop: restart safety ----------------------------------------------


def test_loop_reclaims_orphaned_running_jobs_on_startup():
    # Simulate a job left RUNNING by a worker that crashed before this
    # process started — the whole point of Redis-backed job state.
    redis = _FakeRedis()
    dead_store = _store(redis, lease_seconds=30, max_retries=3)
    dead_store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    orphaned = dead_store.dequeue()  # -> RUNNING, then "the process dies"

    later_store = _store(redis, lease_seconds=30, max_retries=3, now=lambda: NOW + timedelta(seconds=60))

    async def fake_sleep(seconds):
        pass

    asyncio.run(
        run_worker_loop(
            later_store,
            check_fn=None,
            sleep=fake_sleep,
            max_iterations=0,
        )
    )

    stored = later_store.get(orphaned.job_id)
    assert stored.status == JobStatus.RETRYABLE
    assert redis.lists[QUEUE_KEY] == [orphaned.job_id]  # never lost


def test_loop_processes_queued_jobs_until_max_iterations():
    redis = _FakeRedis()
    store = _store(redis, max_retries=3)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    async def fake_check(insurance_id):
        return _FakeResult("active", checked_at=NOW)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    asyncio.run(
        run_worker_loop(
            store,
            check_fn=fake_check,
            sleep=fake_sleep,
            poll_interval_seconds=0.1,
            max_iterations=2,
        )
    )

    assert store.get(created.job_id).status == JobStatus.SUCCEEDED
    # Second iteration found an empty queue and slept instead of busy-looping.
    assert sleeps == [0.1]


def test_loop_respects_stop_event():
    store = _store()

    async def fake_sleep(seconds):
        pass

    calls = {"n": 0}

    async def counting_check(insurance_id):
        calls["n"] += 1
        return _FakeResult("active", checked_at=NOW)

    async def _run():
        stop_event = asyncio.Event()
        stop_event.set()  # already stopped — loop body must not run at all
        await run_worker_loop(store, check_fn=counting_check, sleep=fake_sleep, stop_event=stop_event)

    asyncio.run(_run())

    assert calls["n"] == 0
