"""The one records-service orchestration path: run the agent, validate, persist.

records-service hosts this because it already owns the per-patient grant check,
the clinician review gate and the draft write path (`agent_drafts`).

The order is the safety property: run the bounded agent -> deterministic
validation -> persist. Validation runs BEFORE the clinician sees anything, and a
refusal is terminal for that version, so validation gates rather than advises. A
refused draft is still persisted, deliberately: a refusal that left no row would
make "the agent declined" and "the agent was never asked" look identical.
"""
from dataclasses import dataclass
from typing import Optional

from libs.agent_provenance import ProvenanceLabel, TraceRecorder
from libs.summary_agent import RetrievalLimits, ValidationOutcome, load_corpus, validate_draft
from libs.summary_agent.runtime import run_summary_agent

import agent_drafts
import agent_lifecycle
from config import settings
from logging_config import configure

log = configure(settings.service_name)

DEFAULT_AUDIENCE = "patient"


@dataclass
class GenerationOutcome:
    """What one generation produced. `draft` is the persisted row."""

    draft: object
    validation: ValidationOutcome
    label: str
    trace: TraceRecorder

    @property
    def accepted(self) -> bool:
        return self.validation.passed


def generate_draft(
    db,
    *,
    patient_id: int,
    actor_role: str,
    correlation_id: str,
    audience: str = DEFAULT_AUDIENCE,
    model=None,
    label: Optional[ProvenanceLabel] = None,
    corpus=None,
    limits: Optional[RetrievalLimits] = None,
    trace: Optional[TraceRecorder] = None,
) -> GenerationOutcome:
    """Generate, validate and persist one new draft version for this patient.

    `model=None` uses the real Bedrock model. Any provider problem is handled
    inside `run_summary_agent`, which returns a deterministic `fallback` draft
    rather than raising — so a caller never has to decide what to show when
    Bedrock is down."""
    trace = trace or TraceRecorder(correlation_id)
    result = run_summary_agent(
        audience=audience, actor_role=actor_role, trace=trace, model=model, label=label,
        corpus=corpus or load_corpus(), limits=limits,
    )
    outcome = validate_draft(result.draft, result.ledger)

    draft = agent_drafts.create_draft(
        db, patient_id=patient_id, generated_text=result.draft.summary,
        correlation_id=correlation_id, provenance_label=result.label.value,
        model_id=result.model_id, prompt_version=result.prompt_version,
        citations=result.citations, trace=trace,
    )
    agent_drafts.record_validation(
        db, draft, passed=outcome.passed, validation_code=outcome.refusal_code, trace=trace,
    )
    # W10 Final Stage 4: append every stage this generation accumulated
    # (request, provider_call(s), agent_decision(s), retrieval(s), draft,
    # validation) to the durable lifecycle stream, in the SAME transaction
    # as the draft/validation rows above — commits or rolls back together.
    agent_lifecycle.persist(db, correlation_id, trace.events)
    log.info(
        "summary agent draft (correlation_id=%s patient_id=%s version=%s label=%s passed=%s code=%s)",
        correlation_id, patient_id, draft.version, result.label.value, outcome.passed,
        outcome.refusal_code,
    )
    return GenerationOutcome(draft=draft, validation=outcome, label=result.label.value, trace=trace)
