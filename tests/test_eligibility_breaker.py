"""Tests for the circuit breaker state machine (services/eligibility-service/breaker.py)."""
from conftest import load_module

breaker_mod = load_module("services/eligibility-service/breaker.py", "eligibility_breaker")

CircuitBreaker = breaker_mod.CircuitBreaker
CircuitState = breaker_mod.CircuitState


def _clock():
    now = {"t": 0.0}

    def tick(seconds=0.0):
        now["t"] += seconds

    def clock():
        return now["t"]

    return clock, tick


def test_starts_closed_and_allows_requests():
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=10)

    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow_request() is True


def test_opens_after_consecutive_failure_threshold():
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=10)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED  # not yet at threshold

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=10)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.state == CircuitState.CLOSED  # count restarted after the success


def test_transitions_to_half_open_after_reset_timeout():
    clock, tick = _clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=10, clock=clock)

    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    tick(5)
    assert breaker.state == CircuitState.OPEN  # not yet elapsed

    tick(5)  # total 10 elapsed
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.allow_request() is True


def test_half_open_success_closes_the_breaker():
    clock, tick = _clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=10, clock=clock)

    breaker.record_failure()
    tick(10)
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_success()

    assert breaker.state == CircuitState.CLOSED
    assert breaker.allow_request() is True


def test_half_open_failure_reopens_immediately():
    clock, tick = _clock()
    breaker = CircuitBreaker(failure_threshold=5, reset_timeout_seconds=10, clock=clock)

    breaker.record_failure()  # nowhere near threshold=5, but...
    tick(10)
    # force into half-open by exhausting reset_timeout after a single failure
    # is not enough to have opened it — open it properly first:
    for _ in range(4):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN

    tick(10)
    assert breaker.state == CircuitState.HALF_OPEN

    breaker.record_failure()  # a single failure in half-open reopens it

    assert breaker.state == CircuitState.OPEN
    assert breaker.allow_request() is False
