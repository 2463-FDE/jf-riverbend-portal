"""Stage 1 (feature-readiness) — patient-friendly intake step instructions.

The only place in this package that may call a model, and only to phrase a
plain-language explanation of one already-known wizard step (see
`_STEP_LABELS`). This never receives a patient_id, free-text patient input,
or any field from `IntakeRequest`/`Demographics`/`Insurance` — the prompt
built in `_build_prompt` is a fixed template parameterized only by which of
the four known steps the caller is on, so there is nothing patient-specific
for a model to see, echo, or leak.

Mirrors `libs/patient_view_agent/composer.py`'s bounded-attempt /
deterministic-template-fallback shape: `llm_client=None` skips the model
entirely (0 attempts, template, `used_fallback=False` — there is nothing to
fall back FROM); with a client, at most `max_attempts` bounded
`LLMClient.complete()` calls are made before giving up and using the
template. This never raises.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from libs.llm_client.client import LLMClient
from libs.llm_client.errors import LLMClientError
from libs.safe_logging import get_safe_logger

log = get_safe_logger(__name__)

_DEFAULT_MAX_ATTEMPTS = 2

# The only steps this endpoint (or its caller) ever accepts — a closed set,
# not free text. Keep in sync with services/intake-service/schemas.py's
# _VALID_INTAKE_STEPS and frontend/app/intake/page.tsx's STEPS.
_STEP_LABELS: dict[str, str] = {
    "demographics": "Demographics & Contact",
    "insurance": "Insurance",
    "consents": "Consents",
    "review": "Review & Submit",
}

VALID_STEPS = frozenset(_STEP_LABELS)

_STEP_TEMPLATES: dict[str, str] = {
    "demographics": (
        "This step collects your name, date of birth, and contact information so we "
        "can create your patient record. Your SSN is optional and is only used to "
        "check insurance coverage. If you're unsure about any field, you can ask "
        "front-desk staff and still continue."
    ),
    "insurance": (
        "If you have insurance, enter the carrier and member ID shown on your "
        "insurance card so we can check your coverage. If you don't have your card "
        "handy, or you're a self-pay patient, you can skip this step and continue."
    ),
    "consents": (
        "Please read each consent before agreeing. Consent to treatment and the "
        "privacy notice are required to complete registration; the financial and "
        "communications consents are optional. Ask front-desk staff if anything is "
        "unclear."
    ),
    "review": (
        "Check the information you entered on the previous steps for mistakes. "
        "After you submit, front-desk staff will review your intake before your "
        "visit, and you can still ask them to correct anything afterward."
    ),
}


class ComposedInstructions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str


def _template_instructions(step: str) -> ComposedInstructions:
    return ComposedInstructions(summary=_STEP_TEMPLATES[step])


def _build_prompt(step: str, retry_note: str = "") -> str:
    label = _STEP_LABELS[step]
    instructions = (
        "You are a friendly, non-clinical assistant helping a patient complete a "
        f'new-patient registration form at a community health clinic. In two or '
        f'three short plain-language sentences, explain what the patient needs to '
        f'do on the "{label}" step and why it is needed. Do not give medical '
        f"advice, do not diagnose, and do not ask the patient to repeat any "
        f"personal information back to you. If something might need clarifying, "
        f"tell the patient to ask front-desk staff. Return JSON with exactly one "
        f'field, "summary", containing your answer.'
    )
    return instructions + retry_note


def compose(
    step: str,
    *,
    llm_client: Optional[LLMClient] = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> tuple[ComposedInstructions, int, bool]:
    """Returns `(instructions, attempts, used_fallback)`.

    `step` must already be one of `VALID_STEPS` — callers validate untrusted
    input against that set before calling this (see
    `services/intake-service/schemas.py::IntakeInstructionsRequest`), so an
    unknown step here is a programming error, not a data-validation concern
    for this function to re-check.
    """
    if llm_client is None:
        return _template_instructions(step), 0, False

    retry_note = ""
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            result = llm_client.complete(_build_prompt(step, retry_note), schema=ComposedInstructions)
        except LLMClientError as exc:
            log.warning(
                "intake_instructions compose provider error (step=%s, attempt=%s, error_type=%s)",
                step,
                attempt,
                type(exc).__name__,
            )
            return _template_instructions(step), attempts, True

        if result.summary.strip():
            return result, attempts, False

        log.warning(
            "intake_instructions compose rejected (step=%s, attempt=%s, reason=empty_summary)",
            step,
            attempt,
        )
        retry_note = "\nYour previous answer was empty. Provide a non-empty \"summary\"."

    return _template_instructions(step), attempts, True
