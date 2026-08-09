"""Tests for libs/intake_instructions/composer.py (Stage 1 — feature
readiness): the prompt/template logic behind POST /intake/instructions.

Mirrors libs/patient_view_agent/composer.py's own test shape (bounded
attempts, deterministic template fallback on any provider error, never
raises) using the same FakeProvider-scripting approach as test_llm_client.py.
"""
import pytest
from pydantic import ValidationError

from libs.intake_instructions import VALID_STEPS, ComposedInstructions, compose
from libs.intake_instructions.composer import _STEP_TEMPLATES, _build_prompt
from libs.llm_client import LLMClient, LLMConfig
from libs.llm_client.errors import ProviderTimeoutError
from libs.llm_client.providers.base import ProviderResponse
from libs.llm_client.providers.fake_provider import FakeProvider


def _client(script):
    config = LLMConfig(provider="fake", timeout_seconds=5.0, max_retries=0, max_tokens_per_request=200)
    return LLMClient(config=config, provider=FakeProvider(script), sleep=lambda _s: None)


# --- no client: always the deterministic template, zero attempts -----------


def test_no_llm_client_returns_template_with_zero_attempts_and_no_fallback_flag():
    for step in VALID_STEPS:
        result, attempts, used_fallback = compose(step, llm_client=None)

        assert result.summary == _STEP_TEMPLATES[step]
        assert attempts == 0
        assert used_fallback is False  # nothing to "fall back" from


def test_every_step_has_a_non_empty_template():
    for step in VALID_STEPS:
        assert _STEP_TEMPLATES[step].strip()


# --- model path: success -----------------------------------------------------


def test_model_success_is_returned_as_is():
    script = [ProviderResponse(text='{"summary": "Bring your insurance card."}', input_tokens=5, output_tokens=5)]
    client = _client(script)

    result, attempts, used_fallback = compose("insurance", llm_client=client)

    assert result.summary == "Bring your insurance card."
    assert attempts == 1
    assert used_fallback is False


# --- model path: structured-output validation failure -> retry -> fallback --


def test_unparseable_response_falls_back_to_template_without_retrying():
    # Schema validation happens inside LLMClient.complete() itself and raises
    # StructuredOutputError (an LLMClientError) — same "provider-level
    # failure, don't retry" bucket as a timeout/transient error, not the
    # business-level "retry with a corrective note" path (see the
    # empty-summary test below).
    client = _client([ProviderResponse(text="not json", input_tokens=1, output_tokens=1)])

    result, attempts, used_fallback = compose("demographics", llm_client=client, max_attempts=2)

    assert result.summary == _STEP_TEMPLATES["demographics"]
    assert attempts == 1
    assert used_fallback is True


def test_empty_summary_is_rejected_and_retried_before_falling_back():
    script = [
        ProviderResponse(text='{"summary": ""}', input_tokens=1, output_tokens=1),
        ProviderResponse(text='{"summary": "   "}', input_tokens=1, output_tokens=1),
    ]
    client = _client(script)

    result, attempts, used_fallback = compose("consents", llm_client=client, max_attempts=2)

    assert result.summary == _STEP_TEMPLATES["consents"]
    assert attempts == 2
    assert used_fallback is True


def test_provider_error_falls_back_immediately_without_exhausting_attempts():
    # LLMClient itself already retries transient errors internally (max_retries=0
    # here means it raises on the first failure) — compose() must catch that and
    # degrade to the template rather than letting the exception escape.
    client = _client([ProviderTimeoutError("timed out")])

    result, attempts, used_fallback = compose("review", llm_client=client, max_attempts=2)

    assert result.summary == _STEP_TEMPLATES["review"]
    assert attempts == 1
    assert used_fallback is True


# --- prompt construction: minimum-necessary, no PHI-shaped fields ----------


def test_prompt_never_contains_more_than_the_step_label():
    for step in VALID_STEPS:
        prompt = _build_prompt(step)
        for leaked in ("ssn", "dob", "date_of_birth", "member_id", "notes", "address"):
            assert leaked not in prompt.lower()


def test_prompt_includes_retry_note_when_supplied():
    base = _build_prompt("insurance")
    retried = _build_prompt("insurance", retry_note="\nTry again.")
    assert retried == base + "\nTry again."


# --- ComposedInstructions schema is closed ----------------------------------


def test_composed_instructions_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ComposedInstructions(summary="ok", extra_field="not allowed")
