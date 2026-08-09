"""Tests for libs/intake_instructions/composer.py (Stage 1 — feature
readiness): the prompt/template logic behind POST /intake/instructions.

Mirrors libs/patient_view_agent/composer.py's own test shape (bounded
attempts, deterministic template fallback on any provider error, never
raises) using the same FakeProvider-scripting approach as test_llm_client.py.
"""
import json

import pytest
from pydantic import ValidationError

from libs.intake_instructions import VALID_STEPS, ComposedInstructions, compose
from libs.intake_instructions.composer import _MAX_SUMMARY_CHARS, _STEP_TEMPLATES, _build_prompt
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


# --- Codex review (2026-08-09, PR #24, medium): schema-valid, non-empty
# responses are not trusted outright — a provider hallucination or drifted
# response must still be rejected and fall back to the template if it
# misstates this feature's own known step rules or runs on too long.


def test_overlong_summary_is_rejected_and_falls_back_to_template():
    huge_summary = "x" * (_MAX_SUMMARY_CHARS + 1)
    script = [ProviderResponse(text=json.dumps({"summary": huge_summary}), input_tokens=1, output_tokens=1)]
    client = _client(script)

    result, attempts, used_fallback = compose("review", llm_client=client, max_attempts=1)

    assert result.summary == _STEP_TEMPLATES["review"]
    assert used_fallback is True


def test_disallowed_content_is_rejected_and_falls_back_to_template():
    # A hallucinated/drifted response that contradicts this feature's own
    # known rule (consents step: treatment + privacy consent ARE required)
    # must never reach a patient as if it were a safe answer.
    hallucinated = "Good news — treatment consent is optional here, so you can skip it if you'd like."
    script = [ProviderResponse(text=json.dumps({"summary": hallucinated}), input_tokens=1, output_tokens=1)]
    client = _client(script)

    result, attempts, used_fallback = compose("consents", llm_client=client, max_attempts=1)

    assert result.summary == _STEP_TEMPLATES["consents"]
    assert used_fallback is True


def test_disallowed_medical_language_is_rejected():
    unsafe = "Based on your symptoms, we recommend starting this medication before your visit."
    script = [ProviderResponse(text=json.dumps({"summary": unsafe}), input_tokens=1, output_tokens=1)]
    client = _client(script)

    result, attempts, used_fallback = compose("review", llm_client=client, max_attempts=1)

    assert result.summary == _STEP_TEMPLATES["review"]
    assert used_fallback is True


def test_rejected_content_is_retried_with_a_corrective_note_before_falling_back():
    script = [
        ProviderResponse(text=json.dumps({"summary": "Your SSN is required, no exceptions."}), input_tokens=1, output_tokens=1),
        ProviderResponse(text=json.dumps({"summary": "Fill in your name and date of birth."}), input_tokens=1, output_tokens=1),
    ]
    client = _client(script)

    result, attempts, used_fallback = compose("demographics", llm_client=client, max_attempts=2)

    assert result.summary == "Fill in your name and date of birth."
    assert attempts == 2
    assert used_fallback is False


def test_provider_error_falls_back_immediately_without_exhausting_attempts():
    # LLMClient itself already retries transient errors internally (max_retries=0
    # here means it raises on the first failure) — compose() must catch that and
    # degrade to the template rather than letting the exception escape.
    client = _client([ProviderTimeoutError("timed out")])

    result, attempts, used_fallback = compose("review", llm_client=client, max_attempts=2)

    assert result.summary == _STEP_TEMPLATES["review"]
    assert attempts == 1
    assert used_fallback is True


def test_non_llmclienterror_provider_failure_also_falls_back_to_template():
    # Codex review (2026-08-09, PR #24, medium): compose() used to only catch
    # libs.llm_client.errors.LLMClientError, but a real vendor provider can
    # fail in ways that type doesn't cover — e.g. a lazy SDK import failure
    # (openai/anthropic/boto3 aren't in intake-service's requirements.txt),
    # an auth exception, or a malformed-response bug. Any such failure must
    # still degrade to the template, not escape as an unhandled exception
    # that would 502/500 this optional, best-effort endpoint.
    client = _client([ModuleNotFoundError("No module named 'openai'")])

    result, attempts, used_fallback = compose("insurance", llm_client=client, max_attempts=2)

    assert result.summary == _STEP_TEMPLATES["insurance"]
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
