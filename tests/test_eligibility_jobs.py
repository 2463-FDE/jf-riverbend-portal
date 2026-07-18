"""Unit tests for the Redis-backed eligibility job lifecycle
(services/eligibility-service/jobs.py).

Covers: namespacing, opaque job ids, idempotency, bounded automatic + manual
retries, status TTL refresh, minimized payload shape, and the worker-restart
recovery path (reclaim_expired) that is the whole point of putting job state
in Redis rather than the worker process's memory.
"""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import load_module

jobs_mod = load_module("services/eligibility-service/jobs.py", "eligibility_jobs")

RedisEligibilityJobStore = jobs_mod.RedisEligibilityJobStore
JobStatus = jobs_mod.JobStatus
JobStoreUnavailable = jobs_mod.JobStoreUnavailable
RECORD_PREFIX = jobs_mod.RECORD_PREFIX
QUEUE_KEY = jobs_mod.QUEUE_KEY
IDEMPOTENCY_PREFIX = jobs_mod.IDEMPOTENCY_PREFIX
PROCESSING_KEY = jobs_mod.PROCESSING_KEY

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


class _FakePipeline:
    """Minimal MULTI/EXEC double: buffers commands, then applies them all on
    execute() (redis-py's transaction=True semantics — the server runs the
    queued commands atomically once EXEC is received)."""

    def __init__(self, redis):
        self._redis = redis
        self._ops: list = []

    def set(self, *args, **kwargs):
        self._ops.append(("set", args, kwargs))
        return self

    def lrem(self, *args, **kwargs):
        self._ops.append(("lrem", args, kwargs))
        return self

    def rpush(self, *args, **kwargs):
        self._ops.append(("rpush", args, kwargs))
        return self

    def execute(self):
        results = [getattr(self._redis, name)(*a, **k) for name, a, k in self._ops]
        self._ops = []
        return results


class _FakeRedis:
    """In-memory double covering exactly the redis-py surface jobs.py uses:
    get/set (strings + ex=), rpush/lmove/lrange/lrem (lists), and a
    transaction pipeline (MULTI/EXEC)."""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.lists: dict[str, list] = {}

    def get(self, key):
        return self.strings.get(key)

    def set(self, key, value, ex=None):
        self.strings[key] = value
        if ex is not None:
            self.ttls[key] = ex

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lmove(self, src, dst, src_pos="LEFT", dst_pos="RIGHT"):
        lst = self.lists.get(src)
        if not lst:
            return None
        value = lst.pop(0) if str(src_pos).upper() == "LEFT" else lst.pop()
        dest = self.lists.setdefault(dst, [])
        if str(dst_pos).upper() == "RIGHT":
            dest.append(value)
        else:
            dest.insert(0, value)
        return value

    def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        stop = len(lst) if end == -1 else end + 1
        return list(lst[start:stop])

    def lrem(self, key, count, value):
        lst = self.lists.get(key)
        if not lst:
            return 0
        assert count == 0, "jobs.py only ever calls lrem with count=0"
        kept = [x for x in lst if x != value]
        removed = len(lst) - len(kept)
        self.lists[key] = kept
        return removed

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _RaisingRedis:
    class _Down(Exception):
        pass

    def get(self, key):
        raise self._Down("redis unreachable")

    def set(self, key, value, ex=None):
        raise self._Down("redis unreachable")

    def rpush(self, key, value):
        raise self._Down("redis unreachable")

    def lmove(self, src, dst, src_pos="LEFT", dst_pos="RIGHT"):
        raise self._Down("redis unreachable")

    def lrange(self, key, start, end):
        raise self._Down("redis unreachable")

    def lrem(self, key, count, value):
        raise self._Down("redis unreachable")

    def pipeline(self, transaction=True):
        raise self._Down("redis unreachable")


def _store(redis=None, **kwargs):
    kwargs.setdefault("now", lambda: NOW)
    return RedisEligibilityJobStore(redis or _FakeRedis(), **kwargs)


# --- creation / namespacing / opaque ids -------------------------------------


def test_create_returns_a_queued_job_with_an_opaque_id():
    store = _store()

    job = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    assert job.status == JobStatus.QUEUED
    assert job.insurance_id == "MEM1"
    # Opaque: a uuid4 hex, not derived from insurance_id/patient data.
    assert len(job.job_id) == 32
    assert "MEM1" not in job.job_id


def test_created_job_is_enqueued_for_a_worker_to_pick_up():
    redis = _FakeRedis()
    store = _store(redis)

    job = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    assert redis.lists[QUEUE_KEY] == [job.job_id]


def test_keys_use_dedicated_prefixes_not_shared_with_other_redis_uses():
    redis = _FakeRedis()
    store = _store(redis)

    job = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    record_key = f"{RECORD_PREFIX}{job.job_id}"
    assert record_key in redis.strings
    assert not record_key.startswith("session:")
    assert not record_key.startswith("elig:lkg:")
    assert not record_key.startswith("agent:visit:")
    assert f"{IDEMPOTENCY_PREFIX}k1" in redis.strings


def test_minimized_payload_never_carries_phi_fields():
    store = _store()

    job = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    payload = job.model_dump()
    for phi_field in ("name", "dob", "ssn", "notes", "address", "phone", "email"):
        assert phi_field not in payload


# --- idempotency --------------------------------------------------------------


def test_repeated_create_with_same_idempotency_key_returns_the_same_job():
    redis = _FakeRedis()
    store = _store(redis)

    first = store.create_or_reuse(insurance_id="MEM1", idempotency_key="dup-key")
    second = store.create_or_reuse(insurance_id="MEM1", idempotency_key="dup-key")

    assert first.job_id == second.job_id
    assert redis.lists[QUEUE_KEY] == [first.job_id]  # enqueued exactly once


def test_different_idempotency_keys_create_distinct_jobs():
    store = _store()

    first = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    second = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k2")

    assert first.job_id != second.job_id


def test_create_failure_raises_job_store_unavailable_not_a_bare_exception():
    store = _store(_RaisingRedis())

    with pytest.raises(JobStoreUnavailable):
        store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")


# --- worker lifecycle: dequeue / succeed / fail-or-retry ----------------------


def test_dequeue_claims_the_job_and_sets_a_lease():
    redis = _FakeRedis()
    store = _store(redis, lease_seconds=30)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    claimed = store.dequeue()

    assert claimed.job_id == created.job_id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.lease_expires_at == NOW + timedelta(seconds=30)
    # Atomically moved off the queue and into the processing list.
    assert redis.lists.get(QUEUE_KEY, []) == []
    assert redis.lists[PROCESSING_KEY] == [created.job_id]


def test_dequeue_on_empty_queue_returns_none():
    store = _store()

    assert store.dequeue() is None


def test_dequeue_crash_before_running_write_leaves_a_recoverable_processing_entry():
    # Crash window: the atomic LMOVE claim succeeded (job is in the processing
    # list) but the RUNNING/lease write then failed. The job must NOT be lost:
    # it stays in the processing list with its prior QUEUED record (no lease),
    # which reclaim_expired re-drives — the new design's replacement for the
    # old sadd-based rollback.
    class _RunningWriteFailsRedis(_FakeRedis):
        def set(self, key, value, ex=None):
            if key.startswith(RECORD_PREFIX):
                raise ConnectionError("redis down for the RUNNING write")
            super().set(key, value, ex=ex)

    redis = _FakeRedis()
    store = _store(redis)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    # Swap in the failing client only for the claim, so creation above still
    # persisted the QUEUED record.
    failing = _RunningWriteFailsRedis()
    failing.strings = redis.strings
    failing.ttls = redis.ttls
    failing.lists = redis.lists
    failing_store = _store(failing)

    claimed = failing_store.dequeue()

    assert claimed is None  # never raises; RUNNING write failed
    assert redis.lists.get(QUEUE_KEY, []) == []  # off the queue (LMOVE happened)
    assert redis.lists[PROCESSING_KEY] == [created.job_id]  # but recoverable here
    assert store.get(created.job_id).status == JobStatus.QUEUED  # no stuck RUNNING

    # A later worker's reclaim finds the orphan (no live lease) and re-drives it.
    later = _store(redis, now=lambda: NOW + timedelta(seconds=60))
    reclaimed = later.reclaim_expired()
    assert [j.job_id for j in reclaimed] == [created.job_id]
    assert redis.lists[QUEUE_KEY] == [created.job_id]  # back on the queue — not lost
    assert redis.lists.get(PROCESSING_KEY, []) == []  # cleared from processing


def test_mark_succeeded_records_result_and_clears_processing_entry():
    redis = _FakeRedis()
    store = _store(redis)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    running = store.dequeue()

    store.mark_succeeded(running, result_status="active", result_checked_at=NOW)

    stored = store.get(created.job_id)
    assert stored.status == JobStatus.SUCCEEDED
    assert stored.result_status == "active"
    assert stored.result_checked_at == NOW
    assert created.job_id not in redis.lists.get(PROCESSING_KEY, [])


def test_failed_attempt_under_max_retries_becomes_retryable_and_is_requeued():
    redis = _FakeRedis()
    store = _store(redis, max_retries=2)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    running = store.dequeue()

    result = store.mark_failed_or_retry(running, error_type="RetriesExhaustedError")

    assert result.status == JobStatus.RETRYABLE
    assert result.retry_count == 1
    assert redis.lists[QUEUE_KEY] == [created.job_id]  # back on the queue
    assert created.job_id not in redis.lists.get(PROCESSING_KEY, [])


def test_retry_count_is_bounded_then_dead_letters():
    store = _store(max_retries=2)
    store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    job = store.dequeue()
    job = store.mark_failed_or_retry(job, error_type="err")  # retry 1/2
    assert job.status == JobStatus.RETRYABLE

    job = store.dequeue()
    job = store.mark_failed_or_retry(job, error_type="err")  # retry 2/2
    assert job.status == JobStatus.RETRYABLE

    job = store.dequeue()
    job = store.mark_failed_or_retry(job, error_type="err")  # retries exhausted
    assert job.status == JobStatus.DEAD_LETTER
    assert job.retry_count == 3


def test_dead_lettered_job_is_never_requeued_again():
    redis = _FakeRedis()
    store = _store(redis, max_retries=0)
    store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    job = store.dequeue()

    store.mark_failed_or_retry(job, error_type="err")

    assert redis.lists.get(QUEUE_KEY, []) == []  # not requeued


def test_retry_requeue_is_atomic_no_strand_between_processing_and_queue():
    # The record write, processing-list removal, and re-enqueue must all land
    # together (one MULTI/EXEC), so the job is never observably in neither the
    # queue nor the processing list.
    redis = _FakeRedis()
    store = _store(redis, max_retries=3)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    store.dequeue()

    store.mark_failed_or_retry(store.get(created.job_id), error_type="err")

    assert redis.lists[QUEUE_KEY] == [created.job_id]  # requeued
    assert redis.lists.get(PROCESSING_KEY, []) == []  # and off processing
    assert store.get(created.job_id).status == JobStatus.RETRYABLE


def test_retry_requeue_crash_before_commit_leaves_job_running_and_recoverable():
    # Finding 2: a crash/Redis error during the retry transition must not
    # strand the job. Because the transition is a single MULTI/EXEC, a failure
    # at EXEC applies NONE of it — the job is still a RUNNING record in the
    # processing list (in neither-limbo is impossible), so reclaim re-drives it.
    class _PipelineExecFailsRedis(_FakeRedis):
        def pipeline(self, transaction=True):
            pipe = _FakePipeline(self)

            def _boom():
                raise ConnectionError("redis down at EXEC")

            pipe.execute = _boom
            return pipe

    redis = _PipelineExecFailsRedis()
    store = _store(redis, max_retries=3)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    running = store.dequeue()
    assert running.status == JobStatus.RUNNING

    with pytest.raises(Exception):
        store.mark_failed_or_retry(running, error_type="err")

    # Nothing applied: record still RUNNING, still only in processing.
    assert store.get(created.job_id).status == JobStatus.RUNNING
    assert redis.lists[PROCESSING_KEY] == [created.job_id]
    assert redis.lists.get(QUEUE_KEY, []) == []

    # A later worker (healthy Redis over the same data) reclaims it — not lost.
    healthy = _FakeRedis()
    healthy.strings, healthy.ttls, healthy.lists = redis.strings, redis.ttls, redis.lists
    healthy_store = _store(healthy, max_retries=3, now=lambda: NOW + timedelta(seconds=60))
    reclaimed = healthy_store.reclaim_expired()

    assert [j.job_id for j in reclaimed] == [created.job_id]
    assert healthy.lists[QUEUE_KEY] == [created.job_id]  # back on the queue
    assert healthy.lists.get(PROCESSING_KEY, []) == []  # cleared from processing


# --- controlled manual retry ---------------------------------------------------


def test_manual_retry_requeues_a_dead_lettered_job_once():
    redis = _FakeRedis()
    store = _store(redis, max_retries=0, max_manual_retries=1)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    job = store.dequeue()
    store.mark_failed_or_retry(job, error_type="err")
    assert store.get(created.job_id).status == JobStatus.DEAD_LETTER

    retried = store.retry_manually(created.job_id)

    assert retried.status == JobStatus.RETRYABLE
    assert retried.manual_retry_count == 1
    assert redis.lists[QUEUE_KEY] == [created.job_id]


def test_manual_retry_is_bounded_and_does_not_retry_forever():
    store = _store(max_retries=0, max_manual_retries=1)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    job = store.dequeue()
    store.mark_failed_or_retry(job, error_type="err")

    store.retry_manually(created.job_id)  # consumes the one allowed manual retry
    job = store.dequeue()
    store.mark_failed_or_retry(job, error_type="err")  # dead-letters again

    second_attempt = store.retry_manually(created.job_id)

    assert second_attempt.status == JobStatus.DEAD_LETTER  # unchanged: no retries left
    assert second_attempt.manual_retry_count == 1


def test_manual_retry_on_a_healthy_job_is_a_noop_not_an_error():
    store = _store()
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")  # still QUEUED

    result = store.retry_manually(created.job_id)

    assert result.status == JobStatus.QUEUED  # unchanged, not raised


def test_manual_retry_on_unknown_job_returns_none():
    store = _store()

    assert store.retry_manually("no-such-job") is None


# --- status TTL -----------------------------------------------------------------


def test_job_record_and_idempotency_key_carry_the_configured_status_ttl():
    redis = _FakeRedis()
    store = _store(redis, status_ttl_seconds=900)

    job = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")

    assert redis.ttls[f"{RECORD_PREFIX}{job.job_id}"] == 900
    assert redis.ttls[f"{IDEMPOTENCY_PREFIX}k1"] == 900


# --- worker-restart recovery: never silently lose work ------------------------


def test_reclaim_requeues_a_running_job_whose_lease_expired():
    redis = _FakeRedis()
    store = _store(redis, lease_seconds=30, max_retries=3)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    store.dequeue()  # -> RUNNING, lease = NOW + 30s

    # Simulate the worker dying, then a fresh worker starting up later —
    # past the lease — and running recovery before its normal loop.
    later_store = _store(redis, lease_seconds=30, max_retries=3, now=lambda: NOW + timedelta(seconds=31))

    reclaimed = later_store.reclaim_expired()

    assert len(reclaimed) == 1
    assert reclaimed[0].job_id == created.job_id
    assert reclaimed[0].status == JobStatus.RETRYABLE
    assert reclaimed[0].retry_count == 1
    assert redis.lists[QUEUE_KEY] == [created.job_id]  # back on the queue — not lost


def test_reclaim_does_not_touch_a_running_job_still_within_its_lease():
    redis = _FakeRedis()
    store = _store(redis, lease_seconds=30)
    store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    store.dequeue()

    # Still well within the 30s lease.
    soon_store = _store(redis, lease_seconds=30, now=lambda: NOW + timedelta(seconds=5))
    reclaimed = soon_store.reclaim_expired()

    assert reclaimed == []


def test_reclaim_eventually_dead_letters_a_job_that_keeps_expiring_its_lease():
    redis = _FakeRedis()
    store = _store(redis, lease_seconds=30, max_retries=1)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    store.dequeue()

    # First restart: reclaimed -> RETRYABLE, requeued.
    t1 = _store(redis, lease_seconds=30, max_retries=1, now=lambda: NOW + timedelta(seconds=31))
    t1.reclaim_expired()
    t1.dequeue()  # a "new" worker instance claims it again, then dies again too

    # Second restart, past the second lease: retries exhausted -> DEAD_LETTER.
    t2 = _store(redis, lease_seconds=30, max_retries=1, now=lambda: NOW + timedelta(seconds=65))
    reclaimed = t2.reclaim_expired()

    assert reclaimed[0].job_id == created.job_id
    assert reclaimed[0].status == JobStatus.DEAD_LETTER


def test_reclaim_leaves_a_job_in_processing_when_its_record_read_transiently_fails():
    # A transient read error for one job must NOT be misread as "record gone"
    # and cause the job to be dropped from the processing list — it stays put
    # for the next reclaim tick.
    redis = _FakeRedis()
    store = _store(redis)
    created = store.create_or_reuse(insurance_id="MEM1", idempotency_key="k1")
    store.dequeue()  # -> RUNNING, in processing

    class _RecordReadFailsRedis(_FakeRedis):
        def get(self, key):
            if key.startswith(RECORD_PREFIX):
                raise ConnectionError("redis blip on this read")
            return super().get(key)

    flaky = _RecordReadFailsRedis()
    flaky.strings, flaky.ttls, flaky.lists = redis.strings, redis.ttls, redis.lists
    flaky_store = _store(flaky, now=lambda: NOW + timedelta(seconds=60))

    reclaimed = flaky_store.reclaim_expired()

    assert reclaimed == []  # nothing re-driven this tick
    assert redis.lists[PROCESSING_KEY] == [created.job_id]  # still recoverable
    assert store.get(created.job_id).status == JobStatus.RUNNING  # record intact


def test_reclaim_on_redis_outage_degrades_to_empty_not_an_exception():
    store = _store(_RaisingRedis())

    assert store.reclaim_expired() == []


def test_get_on_redis_outage_degrades_to_none_not_an_exception():
    store = _store(_RaisingRedis())

    assert store.get("some-job") is None
