"""Tests for libs/intake_instructions/composer.py (Stage 1 — feature
readiness): the prompt/selection/template logic behind
POST /intake/instructions.

Mirrors libs/patient_view_agent/composer.py's own test shape (bounded
attempts, deterministic template fallback on any provider error, never
raises) using the same FakeProvider-scripting approach as test_llm_client.py.

Codex review (2026-08-09, PR #24, two rounds): the model used to return free
text, validated first by a non-empty check, then by a length cap plus a
substring denylist — both proved unsound (a paraphrase like "you can skip
the treatment agreement" defeats any denylist). The model's only possible
output is now `variant_index`, selecting among a small, fixed set of
server-authored phrasings per step (`_STEP_VARIANTS`) — there is no field a
model could use to introduce new text at all. Tests below reflect that: the
"model success" cases assert a specific pre-approved variant was selected,
and a dedicated static test asserts every pre-approved variant is itself
free of disallowed language, closing the loop the old runtime denylist could
never fully close against unbounded free text.
"""
import inspect
import json

import pytest
from pydantic import ValidationError

from libs.intake_instructions import VALID_STEPS, ComposedInstructions, compose
from libs.intake_instructions.composer import (
    _STEP_TEMPLATES,
    _STEP_VARIANTS,
    _VariantSelection,
    _build_prompt,
)
from libs.llm_client import LLMClient, LLMConfig
from libs.llm_client.errors import ProviderTimeoutError
from libs.llm_client.providers.base import ProviderResponse
from libs.llm_client.providers.fake_provider import FakeProvider

# A small, fixed set of phrases that must never appear in patient-facing
# intake guidance — checked both statically against every pre-approved
# variant (below) and as a sanity check on the fallback template.
_DISALLOWED_PHRASES = (
    "diagnos",
    "prescri",
    "medication",
    "treatment plan",
    "ssn is required",
    "ssn is mandatory",
    "social security number is required",
    "treatment consent is optional",
    "treatment is optional",
    "privacy notice is optional",
    "privacy consent is optional",
    "all consents are optional",
    "every consent is optional",
    "insurance is required",
)


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


# --- the fixed variant set itself: the only content a patient can ever see -


def test_every_step_has_at_least_two_variants_and_variant_zero_is_the_template():
    for step in VALID_STEPS:
        variants = _STEP_VARIANTS[step]
        assert len(variants) >= 2
        assert variants[0] == _STEP_TEMPLATES[step]


def test_no_pre_approved_variant_contains_disallowed_language():
    # Codex review (2026-08-09, PR #24): this is now an EXHAUSTIVE check —
    # unlike the old runtime denylist against unbounded model free text, the
    # full set of content a patient can ever see is these variants plus the
    # template, all of it fixed and small enough to check completely here.
    for step in VALID_STEPS:
        for variant in _STEP_VARIANTS[step]:
            lowered = variant.lower()
            for phrase in _DISALLOWED_PHRASES:
                assert phrase not in lowered, f"{phrase!r} found in a {step!r} variant: {variant!r}"


# --- model path: success — the model only ever SELECTS, never writes -------


def test_model_success_returns_the_selected_pre_approved_variant():
    script = [ProviderResponse(text=json.dumps({"variant_index": 1}), input_tokens=5, output_tokens=5)]
    client = _client(script)

    result, attempts, used_fallback = compose("insurance", llm_client=client)

    assert result.summary == _STEP_VARIANTS["insurance"][1]
    assert attempts == 1
    assert used_fallback is False


def test_model_selecting_variant_zero_matches_the_template_and_is_not_a_fallback():
    script = [ProviderResponse(text=json.dumps({"variant_index": 0}), input_tokens=5, output_tokens=5)]
    client = _client(script)

    result, attempts, used_fallback = compose("review", llm_client=client)

    assert result.summary == _STEP_TEMPLATES["review"]
    assert used_fallback is False  # a genuine selection, not a degraded fallback


# --- model path: invalid selection -> retry -> fallback --------------------


def test_unparseable_response_falls_back_to_template_without_retrying():
    # Schema validation happens inside LLMClient.complete() itself and raises
    # StructuredOutputError (an LLMClientError) — same "provider-level
    # failure, don't retry" bucket as a timeout/transient error.
    client = _client([ProviderResponse(text="not json", input_tokens=1, output_tokens=1)])

    result, attempts, used_fallback = compose("demographics", llm_client=client, max_attempts=2)

    assert result.summary == _STEP_TEMPLATES["demographics"]
    assert attempts == 1
    assert used_fallback is True


def test_out_of_range_variant_index_is_rejected_and_retried_before_falling_back():
    script = [
        ProviderResponse(text=json.dumps({"variant_index": 99}), input_tokens=1, output_tokens=1),
        ProviderResponse(text=json.dumps({"variant_index": 200}), input_tokens=1, output_tokens=1),
    ]
    client = _client(script)

    result, attempts, used_fallback = compose("consents", llm_client=client, max_attempts=2)

    assert result.summary == _STEP_TEMPLATES["consents"]
    assert attempts == 2
    assert used_fallback is True


def test_negative_variant_index_is_rejected():
    client = _client([ProviderResponse(text=json.dumps({"variant_index": -1}), input_tokens=1, output_tokens=1)])

    result, attempts, used_fallback = compose("review", llm_client=client, max_attempts=1)

    assert result.summary == _STEP_TEMPLATES["review"]
    assert used_fallback is True


def test_invalid_selection_is_retried_with_a_corrective_note_then_succeeds():
    script = [
        ProviderResponse(text=json.dumps({"variant_index": 99}), input_tokens=1, output_tokens=1),
        ProviderResponse(text=json.dumps({"variant_index": 1}), input_tokens=1, output_tokens=1),
    ]
    client = _client(script)

    result, attempts, used_fallback = compose("demographics", llm_client=client, max_attempts=2)

    assert result.summary == _STEP_VARIANTS["demographics"][1]
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


# --- prompt construction: minimum-necessary, no patient-data parameters ----


def test_build_prompt_accepts_no_patient_data_parameters():
    # Structural guarantee, not just a string check: _build_prompt's own
    # signature proves no patient_id/demographics/insurance object can ever
    # reach it, regardless of what future code calls it.
    assert list(inspect.signature(_build_prompt).parameters) == ["step", "retry_note"]


def test_prompt_includes_retry_note_when_supplied():
    base = _build_prompt("insurance")
    retried = _build_prompt("insurance", retry_note="\nTry again.")
    assert retried == base + "\nTry again."


def test_prompt_lists_every_variant_by_index():
    prompt = _build_prompt("consents")
    for i, variant in enumerate(_STEP_VARIANTS["consents"]):
        assert f"{i}: {variant}" in prompt


# --- schemas are closed ------------------------------------------------------


def test_composed_instructions_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ComposedInstructions(summary="ok", extra_field="not allowed")


def test_variant_selection_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        _VariantSelection(variant_index=0, summary="not allowed")


def test_variant_selection_has_no_field_for_free_text():
    # Codex review (2026-08-09, PR #24): the core guarantee this whole
    # redesign rests on — assert it structurally, not just by convention.
    assert set(_VariantSelection.model_fields) == {"variant_index"}
