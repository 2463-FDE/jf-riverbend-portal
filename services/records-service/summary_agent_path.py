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
from libs.policy_corpus import RetrievalScope
from libs.summary_agent import RetrievalLimits, ValidationOutcome, validate_draft
from libs.summary_agent.runtime import run_summary_agent

import agent_drafts
import agent_lifecycle
import bedrock_usage
from config import settings
from logging_config import configure

log = configure(settings.service_name)

DEFAULT_AUDIENCE = "patient"
_WORKFLOW = "patient_summary"
_PROVIDER = "bedrock"
_USE_CASE = "summary_agent_chat"


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


def _policy_connection():
    # Lazy — mirrors policy_navigator_path.py::_policy_connection: importing
    # this module must never require psycopg2/pgvector installed; only
    # actually connecting does.
    import psycopg2
    from pgvector.psycopg2 import register_vector

    conn = psycopg2.connect(
        host=settings.db_host, port=settings.db_port, dbname=settings.db_name,
        user=settings.db_user, password=settings.db_password,
    )
    register_vector(conn)
    return conn


def _build_retriever():
    """The real PolicyRetriever plus the connection it owns, or (None, None)
    if retrieval infrastructure is unavailable. Mirrors
    policy_navigator_path.py's PN-CONN-LEAK-fixed order: the embedding
    provider is validated BEFORE any Postgres connection is opened.
    `retrieve()`'s own guard treats retriever=None as zero chunks rather
    than raising — the same safe-empty-evidence path a mid-call network
    failure already takes."""
    import os

    from libs.embedding_client import EmbeddingClient, EmbeddingConfig
    from libs.policy_corpus import BedrockPolicyEmbeddingProvider, PolicyRetriever

    model_id = os.getenv("POLICY_EMBEDDING_MODEL_ID", "")
    try:
        embedding_client = EmbeddingClient(
            config=EmbeddingConfig(provider=_PROVIDER),
            provider=BedrockPolicyEmbeddingProvider(model_id=model_id or None),
        )
        conn = _policy_connection()
    except Exception as exc:
        log.warning("summary agent retrieval infrastructure unavailable (error_type=%s)", type(exc).__name__)
        return None, None

    from pgvector import Vector  # lazy — see _policy_connection

    return PolicyRetriever(conn, embedding_client, provider=_PROVIDER, model=model_id, vector_cast=Vector), conn


def generate_draft(
    db,
    *,
    patient_id: int,
    actor_role: str,
    correlation_id: str,
    audience: str = DEFAULT_AUDIENCE,
    model=None,
    label: Optional[ProvenanceLabel] = None,
    retriever=None,
    limits: Optional[RetrievalLimits] = None,
    trace: Optional[TraceRecorder] = None,
) -> GenerationOutcome:
    """Generate, validate and persist one new draft version for this patient.

    `model=None` uses the real Bedrock model. Any provider problem is handled
    inside `run_summary_agent`, which returns a deterministic `fallback` draft
    rather than raising — so a caller never has to decide what to show when
    Bedrock is down.

    `retriever=None` (the normal case) builds the real pgvector-backed
    PolicyRetriever; a caller (a test) may inject its own instead, the same
    seam `model` already offers. `scope` — which audiences/workflows this
    summary may draw from — is fixed here, by trusted application code,
    never derived from a model argument.
    """
    trace = trace or TraceRecorder(correlation_id)
    scope = RetrievalScope(audiences=(audience,), workflows=(_WORKFLOW,))
    conn = None
    if retriever is None:
        retriever, conn = _build_retriever()
    try:
        result = run_summary_agent(
            scope=scope, retriever=retriever, actor_role=actor_role, trace=trace, model=model,
            label=label, limits=limits,
        )
    finally:
        if conn is not None:
            conn.close()
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
    # W10 Final Stage 5 sub-slice 3: durable usage accounting for whichever
    # turns genuinely called a real Bedrock model — empty whenever no real
    # model was ever configured/reached, never invented.
    bedrock_usage.persist(db, correlation_id, [
        bedrock_usage.UsageEvent(
            provider=_PROVIDER, model_id=turn.model_id, use_case=_USE_CASE, sequence=turn.turn,
            input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
        )
        for turn in result.usage
    ])
    log.info(
        "summary agent draft (correlation_id=%s patient_id=%s version=%s label=%s passed=%s code=%s)",
        correlation_id, patient_id, draft.version, result.label.value, outcome.passed,
        outcome.refusal_code,
    )
    return GenerationOutcome(draft=draft, validation=outcome, label=result.label.value, trace=trace)
