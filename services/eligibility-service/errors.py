"""Exception hierarchy for the payer client.

Mirrors the shape of libs/llm_client/errors.py (timeout/transient failures are
retry-classified; the client exhausts its own retries and raises a single
"gave up" error) but is defined locally here per adr/0001 — there is no shared
Python package across services yet.
"""


class PayerClientError(Exception):
    """Base class for all payer-client-related errors."""


class PayerTimeoutError(PayerClientError):
    """A single payer call exceeded its allotted timeout. Safe to retry."""


class PayerTransientError(PayerClientError):
    """A payer call failed in a way that's safe to retry (connection reset, DNS blip, etc.)."""


class RetriesExhaustedError(PayerClientError):
    """PayerClient used up all configured retries without a successful response."""


class CircuitOpenError(PayerClientError):
    """Labels EligibilityResult.error_type when the circuit breaker denies a
    call outright. check.py branches on CircuitBreaker.allow_request()'s
    return value directly rather than raising/catching this — it exists so
    that "why did this degrade" has one named, greppable value either way.
    """
