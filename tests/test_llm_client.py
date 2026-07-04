"""Tests for the provider-swappable LLM client (libs/llm_client).

All tests run against `FakeProvider` (libs/llm_client/providers/fake_provider.py)
or a hand-scripted `Provider` stub — never a real openai/anthropic/ollama call,
and never require an API key. `LLMClient` is always constructed with an
injected no-op `sleep` so retry/backoff tests run instantly and don't touch
`time.sleep`.
"""
import logging

import pytest
from pydantic import BaseModel

from libs.llm_client import (
    BudgetExceededError,
    LLMClient,
    LLMConfig,
    LLMRetriesExhaustedError,
    StructuredOutputError,
)
from libs.llm_client.errors import ProviderTimeoutError, ProviderTransientError
from libs.llm_client.providers.base import ProviderResponse
from libs.llm_client.providers.fake_provider import FakeProvider

# A string that looks like the kind of thing that must never reach a log line:
# a fake SSN embedded in a "prompt" and echoed back in a "response".
PHI_MARKER = "ssn=111-22-3333"


def _client(script, **config_overrides):
    sleeps = []
    config = LLMConfig(
        provider="fake",
        timeout_seconds=config_overrides.pop("timeout_seconds", 5.0),
        max_retries=config_overrides.pop("max_retries", 3),
        max_tokens_per_request=config_overrides.pop("max_tokens_per_request", 2000),
        monthly_token_budget=config_overrides.pop("monthly_token_budget", 1_000_000),
    )
    assert not config_overrides, f"unhandled overrides: {config_overrides}"
    provider = FakeProvider(script)
    client = LLMClient(config=config, provider=provider, sleep=lambda seconds: sleeps.append(seconds))
    return client, provider, sleeps


# --- retry / backoff -------------------------------------------------------


def test_succeeds_after_transient_errors_and_backs_off_between_attempts():
    script = [
        ProviderTransientError("rate limited"),
        ProviderTimeoutError("timed out"),
        ProviderResponse(text="ok", input_tokens=10, output_tokens=5),
    ]
    client, provider, sleeps = _client(script, max_retries=3)

    result = client.complete("prompt")

    assert result == "ok"
    assert len(provider.calls) == 3
    # one backoff sleep between each failed attempt and the next
    assert len(sleeps) == 2
    assert all(0 <= s <= 8.0 for s in sleeps)


def test_backoff_delay_grows_with_attempt_number_and_is_capped():
    delays_by_attempt = [LLMClient._backoff_delay(0) for _ in range(200)]
    later_delays = [LLMClient._backoff_delay(4) for _ in range(200)]

    assert max(delays_by_attempt) <= 0.5
    assert max(later_delays) <= 8.0
    # attempt 4 should be able to reach well past attempt 0's ceiling
    assert max(later_delays) > max(delays_by_attempt)


def test_retries_exhausted_raises_and_stops_at_configured_max():
    script = [ProviderTransientError("down")] * 10  # more failures than max_retries+1 needs
    client, provider, sleeps = _client(script, max_retries=2)

    with pytest.raises(LLMRetriesExhaustedError):
        client.complete("prompt")

    assert len(provider.calls) == 3  # initial attempt + 2 retries
    assert len(sleeps) == 2  # no sleep after the final, non-retried failure


def test_non_retryable_provider_error_is_not_caught():
    script = [ValueError("misconfigured adapter")]
    client, provider, sleeps = _client(script, max_retries=3)

    with pytest.raises(ValueError):
        client.complete("prompt")

    assert len(provider.calls) == 1
    assert sleeps == []


# --- timeout handling --------------------------------------------------


def test_timeout_error_is_retried_like_a_transient_error():
    script = [
        ProviderTimeoutError("timed out"),
        ProviderResponse(text="ok", input_tokens=1, output_tokens=1),
    ]
    client, provider, sleeps = _client(script, max_retries=1)

    assert client.complete("prompt") == "ok"
    assert len(provider.calls) == 2


def test_default_timeout_is_passed_to_provider():
    client, provider, _ = _client(
        [ProviderResponse(text="ok", input_tokens=1, output_tokens=1)], timeout_seconds=12.5
    )

    client.complete("prompt")

    assert provider.calls[0]["timeout"] == 12.5


def test_per_call_timeout_override_is_passed_to_provider():
    client, provider, _ = _client(
        [ProviderResponse(text="ok", input_tokens=1, output_tokens=1)], timeout_seconds=30.0
    )

    client.complete("prompt", timeout=2.0)

    assert provider.calls[0]["timeout"] == 2.0


def test_all_attempts_time_out_raises_retries_exhausted():
    script = [ProviderTimeoutError("timed out")] * 5
    client, provider, _ = _client(script, max_retries=2)

    with pytest.raises(LLMRetriesExhaustedError):
        client.complete("prompt")

    assert len(provider.calls) == 3


# --- structured-output parsing ------------------------------------------


class Diagnosis(BaseModel):
    code: str
    confidence: float


def test_structured_output_parses_valid_json_into_schema():
    client, _, _ = _client(
        [ProviderResponse(text='{"code": "R51", "confidence": 0.9}', input_tokens=5, output_tokens=5)]
    )

    result = client.complete("prompt", schema=Diagnosis)

    assert isinstance(result, Diagnosis)
    assert result.code == "R51"
    assert result.confidence == 0.9


def test_structured_output_invalid_json_raises_structured_output_error():
    client, _, _ = _client(
        [ProviderResponse(text="not json at all", input_tokens=5, output_tokens=5)]
    )

    with pytest.raises(StructuredOutputError):
        client.complete("prompt", schema=Diagnosis)


def test_structured_output_valid_json_wrong_shape_raises_structured_output_error():
    client, _, _ = _client(
        [ProviderResponse(text='{"unexpected": "shape"}', input_tokens=5, output_tokens=5)]
    )

    with pytest.raises(StructuredOutputError):
        client.complete("prompt", schema=Diagnosis)


def test_no_schema_returns_raw_text():
    client, _, _ = _client([ProviderResponse(text="plain text reply", input_tokens=1, output_tokens=1)])

    assert client.complete("prompt") == "plain text reply"


# --- token / cost guard --------------------------------------------------


def test_budget_already_exhausted_blocks_call_before_provider_is_invoked():
    client, provider, _ = _client(
        [ProviderResponse(text="ok", input_tokens=1, output_tokens=1)], monthly_token_budget=100
    )
    client._tokens_used = 100  # simulate prior calls this billing period

    with pytest.raises(BudgetExceededError):
        client.complete("prompt")

    assert provider.calls == []  # never even attempted


def test_call_that_pushes_usage_over_budget_raises_after_the_call():
    client, provider, _ = _client(
        [ProviderResponse(text="ok", input_tokens=60, output_tokens=50)], monthly_token_budget=100
    )

    with pytest.raises(BudgetExceededError):
        client.complete("prompt")

    # the call itself still happened — the guard is a post-call trip wire
    assert len(provider.calls) == 1
    assert client._tokens_used == 110


def test_call_within_budget_succeeds_and_accumulates_usage():
    client, _, _ = _client(
        [ProviderResponse(text="ok", input_tokens=10, output_tokens=10)], monthly_token_budget=1000
    )

    client.complete("prompt")

    assert client._tokens_used == 20


# --- PHI-safe logging behavior of the client ------------------------------


def _log_text(caplog):
    return "\n".join(record.getMessage() for record in caplog.records)


def test_successful_call_logs_no_raw_prompt_or_response_text(caplog):
    caplog.set_level(logging.INFO, logger="libs.llm_client.client")
    client, _, _ = _client(
        [ProviderResponse(text=f"model reply containing {PHI_MARKER}", input_tokens=3, output_tokens=4)]
    )

    client.complete(f"prompt containing {PHI_MARKER}")

    text = _log_text(caplog)
    assert PHI_MARKER not in text


def test_retry_logging_contains_no_raw_prompt_and_no_raw_exception_message(caplog):
    caplog.set_level(logging.INFO, logger="libs.llm_client.client")
    script = [
        ProviderTransientError(f"upstream error echoing {PHI_MARKER}"),
        ProviderResponse(text="ok", input_tokens=1, output_tokens=1),
    ]
    client, _, _ = _client(script, max_retries=2)

    client.complete(f"prompt containing {PHI_MARKER}")

    text = _log_text(caplog)
    assert PHI_MARKER not in text
    assert "ProviderTransientError" in text  # exception TYPE name is fine to log


def test_retries_exhausted_logging_contains_no_raw_prompt_or_exception_message(caplog):
    caplog.set_level(logging.INFO, logger="libs.llm_client.client")
    script = [ProviderTransientError(f"upstream error echoing {PHI_MARKER}")] * 3
    client, _, _ = _client(script, max_retries=1)

    with pytest.raises(LLMRetriesExhaustedError):
        client.complete(f"prompt containing {PHI_MARKER}")

    text = _log_text(caplog)
    assert PHI_MARKER not in text


def test_structured_output_failure_logging_contains_no_raw_response_text(caplog):
    caplog.set_level(logging.INFO, logger="libs.llm_client.client")
    client, _, _ = _client(
        [ProviderResponse(text=f"not json, but has {PHI_MARKER}", input_tokens=1, output_tokens=1)]
    )

    with pytest.raises(StructuredOutputError):
        client.complete("prompt", schema=Diagnosis)

    text = _log_text(caplog)
    assert PHI_MARKER not in text
    assert "structured output validation failed" in text
