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
template. This never raises: `compose()` catches any exception from
`llm_client.complete()` (Codex review, 2026-08-09, PR #24), not just
`libs.llm_client.errors.LLMClientError` — a real vendor provider can fail in
ways that error type doesn't cover (a lazy SDK import failure, an auth
exception, a malformed response), and this optional, best-effort endpoint
must degrade to its template on every one of them, not just the ones
`llm_client`'s own adapters happen to normalize.

Codex review (2026-08-09, PR #24, two more rounds): earlier revisions let the
model return arbitrary free text validated by a non-empty check, then by a
length cap plus a substring denylist. Both are unsound for rule-bearing
content shown to a patient in a healthcare intake flow — a paraphrase (e.g.
"you can skip the treatment agreement") defeats any denylist, and there is
no bound on what a hallucinating or drifted provider might otherwise claim
about a required consent, an SSN, or medical advice. The model NEVER
generates the text a patient sees. Instead, `_STEP_VARIANTS` holds a small,
fixed set of server-authored, human-reviewed phrasings per step (element 0
is always `_STEP_TEMPLATES[step]`, the same deterministic fallback text);
the model's only output is `_VariantSelection.variant_index`, an integer
picking which pre-approved phrasing to show. There is structurally no path
for the model to introduce a new factual claim, required/optional claim, or
medical statement — an invalid index is rejected exactly like the old
empty/malformed cases and retried, then falls back to variant 0 (the
template).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from libs.llm_client.client import LLMClient
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

# Server-authored, human-reviewed phrasing options per step. Element 0 is
# always _STEP_TEMPLATES[step] verbatim, so the deterministic fallback and
# "the model picked variant 0" produce identical, already-tested text. Adding
# a variant means writing and reviewing new text HERE, in source control —
# never accepting one generated at request time.
_STEP_VARIANTS: dict[str, tuple[str, ...]] = {
    "demographics": (
        _STEP_TEMPLATES["demographics"],
        (
            "We need your name, date of birth, and a way to reach you to set up your "
            "chart. Your SSN is optional and only used for insurance — front-desk "
            "staff can help if you're missing anything."
        ),
    ),
    "insurance": (
        _STEP_TEMPLATES["insurance"],
        (
            "Got insurance? Add the carrier and member ID from your card so we can "
            "check your coverage. No insurance, or paying yourself? You can skip "
            "this step and keep going."
        ),
    ),
    "consents": (
        _STEP_TEMPLATES["consents"],
        (
            "Take a moment to read each consent below. Treatment and the privacy "
            "notice must be accepted to register; the financial and communications "
            "consents are up to you. Ask front-desk staff first if anything's unclear."
        ),
    ),
    "review": (
        _STEP_TEMPLATES["review"],
        (
            "Look over what you entered — this is your last chance to fix a typo "
            "before you submit. Front-desk staff will still review it, and you can "
            "always correct something afterward."
        ),
    ),
}


class ComposedInstructions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str


class _VariantSelection(BaseModel):
    """The model's ONLY possible output: which pre-approved phrasing to show.
    There is no field here a model could use to supply its own text."""

    model_config = ConfigDict(extra="forbid")

    variant_index: int


def _template_instructions(step: str) -> ComposedInstructions:
    return ComposedInstructions(summary=_STEP_TEMPLATES[step])


def _build_prompt(step: str, retry_note: str = "") -> str:
    label = _STEP_LABELS[step]
    variants = _STEP_VARIANTS[step]
    numbered_options = "\n".join(f"{i}: {text}" for i, text in enumerate(variants))
    instructions = (
        "You are helping pick the clearest, most patient-friendly wording for the "
        f'"{label}" step of a clinic new-patient registration form. Every option '
        "below has already been written and reviewed for accuracy — choose the ONE "
        "that reads best to a patient. Do not write your own wording, and do not "
        'combine options. Return JSON with exactly one field, "variant_index", the '
        f"integer index (0 to {len(variants) - 1}) of your chosen option.\n\n"
        f"{numbered_options}"
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

    variants = _STEP_VARIANTS[step]
    retry_note = ""
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            selection = llm_client.complete(_build_prompt(step, retry_note), schema=_VariantSelection)
        except Exception as exc:
            # Deliberately broader than `except LLMClientError` — see the
            # module docstring's first Codex-review note. Every provider
            # failure mode degrades the same way; only the exception TYPE is
            # ever logged.
            log.warning(
                "intake_instructions compose provider error (step=%s, attempt=%s, error_type=%s)",
                step,
                attempt,
                type(exc).__name__,
            )
            return _template_instructions(step), attempts, True

        if 0 <= selection.variant_index < len(variants):
            return ComposedInstructions(summary=variants[selection.variant_index]), attempts, False

        log.warning(
            "intake_instructions compose rejected (step=%s, attempt=%s, reason=invalid_variant_index)",
            step,
            attempt,
        )
        retry_note = (
            f"\nYour previous answer was invalid. Return a variant_index "
            f"between 0 and {len(variants) - 1}, inclusive."
        )

    return _template_instructions(step), attempts, True
