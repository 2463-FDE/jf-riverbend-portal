"""The agentic draft write path: create -> validate -> approve -> display.

Four transitions, and the ordering between them is the safety property. A draft
is only ever shown to a patient when it has been validated AND approved, and
what is shown is the exact stored text of that version — never a regeneration.
`adr/0010` records why the text is persisted at all.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

It does not call a model. `create_draft` takes text that has already been
produced, with the label saying where it came from (`real`, `fixture`,
`fallback`). Keeping generation out means the state machine is testable without
Bedrock, which matters because the region and model id have not arrived and the
freeze has not moved.

It also does not emit `retrieval`, `provider_call` or `agent_decision` trace
stages, on the SAME grounds: this module persists what an orchestration layer
already decided and produced, it does not perform or re-narrate that work.
Only whichever runtime actually ran the model call and the tool lookups may
honestly emit those three stages — see `libs/agent_provenance.Stage`'s
docstring for the exact shape, grounded in
`libs/eligibility_agent/runtimes/raw_bedrock.py` /`langchain_runtime.py`. Every
function below emits at most the ONE stage its own transition corresponds to
(`create_draft` -> `draft`, `record_validation` -> `validation`, `decide` ->
`review`, `approved_draft` -> `display`).

`generated_text` is PERSISTED PHI and is AEAD-encrypted the instant a draft
is created (adr/0012 follow-up, migration 032, `phi.encrypt_draft_text`) —
`create_draft` below never writes plaintext, even transiently. Nothing here
may pass draft text to a logger, a span, or a prompt — `libs.agent_provenance`
raises if anything tries, and the log lines below carry ids and codes only.
"""
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import phi
from libs.agent_provenance import ProvenanceLabel, TraceRecorder
from libs.metrics import ai as ai_metrics
from logging_config import configure
from models import AgentDraftCitation, AgentDraftProvenance
from config import settings

log = configure(settings.service_name)

# Statuses, mirroring migration 020's CHECK. Named here so callers branch on a
# constant rather than a string literal that can drift from the constraint.
DRAFT = "draft"
VALIDATED = "validated"
REFUSED = "refused"
APPROVED = "approved"
REJECTED = "rejected"
SUPERSEDED = "superseded"

LABEL_REAL = "real"
LABEL_FIXTURE = "fixture"
LABEL_FALLBACK = "fallback"
LABELS = (LABEL_REAL, LABEL_FIXTURE, LABEL_FALLBACK)

# The one machine-readable code every non-refused post-validation status
# carries, per migration 020's agent_draft_validation_code_consistent CHECK.
# Named here so record_validation branches on a constant, not a literal that
# can drift from the constraint.
VALIDATION_PASS_CODE = "PASS"


class DraftError(Exception):
    """A refused transition. Carries no draft text, deliberately."""


def next_version(db: Session, patient_id: int) -> int:
    """Monotonic per patient. Never reuses a version, even after a rejection —
    a rejected version stays on the record as a thing that was rejected."""
    current = db.execute(
        select(func.max(AgentDraftProvenance.version)).where(
            AgentDraftProvenance.patient_id == patient_id
        )
    ).scalar()
    return (current or 0) + 1


def create_draft(
    db: Session,
    *,
    patient_id: int,
    generated_text: str,
    correlation_id: str,
    provenance_label: str,
    model_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    citations: Iterable[dict] = (),
    trace: Optional[TraceRecorder] = None,
) -> AgentDraftProvenance:
    """Persist a new draft version and its citations.

    `provenance_label` is required and validated: an unlabelled draft could later
    be displayed without the caller knowing whether a model produced it, and the
    client requires that distinction to be explicit on screen.
    """
    if provenance_label not in LABELS:
        raise DraftError(f"provenance_label must be one of {LABELS}, got {provenance_label!r}")
    if not generated_text or not generated_text.strip():
        raise DraftError("a draft with no text is not a draft")
    if provenance_label == LABEL_FALLBACK and model_id:
        # A fallback did not call a model, so naming one would misattribute the
        # text — the exact confusion the labels exist to prevent.
        raise DraftError("a fallback draft must not name a model_id")

    cite_list = list(citations)  # materialize once: used for both the DB rows and the trace

    # adr/0012 follow-up (migration 032): encrypted the instant a draft is
    # created — never persisted plaintext even transiently. AAD binds the
    # ciphertext to (patient_id, version), the row's own immutable identity
    # (agent_draft_provenance_guard_trigger), computed here so it can be
    # used both for the encryption call and the row itself.
    draft_version = next_version(db, patient_id)
    envelope, key_version = phi.encrypt_draft_text(patient_id, draft_version, generated_text)

    draft = AgentDraftProvenance(
        patient_id=patient_id,
        version=draft_version,
        status=DRAFT,
        provenance_label=provenance_label,
        correlation_id=correlation_id,
        model_id=model_id,
        prompt_version=prompt_version,
        generated_text=envelope,
        generated_text_key_version=key_version,
    )
    db.add(draft)
    db.flush()  # assign draft.id before the citations reference it

    for c in cite_list:
        db.add(
            AgentDraftCitation(
                draft_id=draft.id,
                source_id=c["source_id"],
                source_version=c["source_version"],
                citation_id=c["citation_id"],
                category=c.get("category"),
            )
        )
    db.flush()
    log.info(
        "agent draft created (correlation_id=%s patient_id=%s version=%s label=%s)",
        correlation_id, patient_id, draft.version, provenance_label,
    )
    if trace is not None:
        # ONLY the draft stage — by reference (version, label, model/prompt
        # version, citation ids), never the text.
        #
        # This function does NOT emit retrieval, provider_call or
        # agent_decision. Those stages belong to whichever orchestration or
        # runtime actually performed the model call and the document
        # lookups — libs/eligibility_agent/runtimes/raw_bedrock.py and
        # langchain_runtime.py are the reference shape (see
        # libs/agent_provenance.Stage's docstring) — and that work happens
        # strictly BEFORE this function is ever invoked. create_draft is a
        # persistence step, not the agent loop; it must not fabricate stages
        # for work it did not itself do. An earlier version of this function
        # emitted retrieval/provider_call here, which double-counted (or
        # invented, when called from a context with no real retrieval at
        # all) stages that the caller's own orchestration is responsible for.
        trace.draft(
            draft_version=draft.version,
            label=ProvenanceLabel(provenance_label),
            model_id=model_id,
            prompt_version=prompt_version,
            citation_ids=[c["citation_id"] for c in cite_list],
        )
    return draft


def record_validation(db: Session, draft: AgentDraftProvenance, *, passed: bool,
                      validation_code: Optional[str] = None,
                      trace: Optional[TraceRecorder] = None) -> AgentDraftProvenance:
    """Deterministic validation's verdict.

    A refusal is terminal for that version: `refused` is not a state a review can
    approve out of. Regenerating produces a new version, which is the point of
    versioning.

    `validation_code` is only meaningful for a REFUSAL: a passing validation
    always records `VALIDATION_PASS_CODE` (migration 020's
    agent_draft_validation_code_consistent CHECK requires exactly this for
    every non-refused post-validation status, and leaving it caller-supplied —
    or defaulting to None, as this used to — either drifts from that constant
    or writes NULL, both of which the constraint now rejects outright). A
    refusal must carry its own specific, non-blank, non-PASS reason code, or
    "refused, no reason" would reach the review queue unexplained.
    """
    if draft.status != DRAFT:
        raise DraftError(
            f"only a {DRAFT!r} draft can be validated; version {draft.version} is {draft.status!r}"
        )
    if passed:
        if validation_code is not None and validation_code != VALIDATION_PASS_CODE:
            raise DraftError(
                f"a passing validation always records {VALIDATION_PASS_CODE!r}; "
                f"got {validation_code!r} — do not pass a differing code"
            )
        code = VALIDATION_PASS_CODE
    else:
        if not validation_code or not validation_code.strip():
            raise DraftError("a refusal must carry a specific, non-blank refusal code")
        if validation_code == VALIDATION_PASS_CODE:
            raise DraftError(f"a refusal code cannot be {VALIDATION_PASS_CODE!r}")
        code = validation_code

    draft.status = VALIDATED if passed else REFUSED
    draft.validation_code = code
    db.flush()
    log.info(
        "agent draft validation (correlation_id=%s version=%s passed=%s code=%s)",
        draft.correlation_id, draft.version, passed, code,
    )
    if trace is not None:
        trace.validation(
            passed=passed,
            validation_code=code,
            citation_ids=[c.citation_id for c in citations_for(db, draft.id)],
        )
    return draft


def _seconds_awaiting_review(draft) -> Optional[float]:
    """How long the draft waited between generation and this decision.

    `approved_at`/`rejected_at` are set to `func.now()`, so right after the
    flush they are still SQL expressions rather than datetimes — the elapsed
    time is measured against the application clock instead. Returns None on
    anything unexpected (missing or naive-vs-aware `created_at`, a clock
    skew producing a negative interval) so a bad reading is simply not
    observed rather than becoming a nonsense bucket.
    """
    created_at = getattr(draft, "created_at", None)
    if not isinstance(created_at, datetime):
        return None
    try:
        reference = created_at
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - reference).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    return elapsed if elapsed >= 0 else None


def decide(db: Session, draft: AgentDraftProvenance, *, approve: bool,
           reviewed_by: int,
           trace: Optional[TraceRecorder] = None) -> AgentDraftProvenance:
    """The clinician gate. Points at THIS version, by construction.

    Only a validated draft is decidable. A refused draft cannot be approved —
    otherwise deterministic validation would be advisory, and the client's
    requirement is that it gate.
    """
    if draft.status != VALIDATED:
        raise DraftError(
            f"only a {VALIDATED!r} draft can be decided; version {draft.version} "
            f"is {draft.status!r}"
        )
    if approve:
        # Supersede any previously approved version FIRST: two approved versions
        # would make "the approved version" ambiguous, and display would have to
        # guess.
        db.execute(
            AgentDraftProvenance.__table__.update()
            .where(
                AgentDraftProvenance.patient_id == draft.patient_id,
                AgentDraftProvenance.status == APPROVED,
                AgentDraftProvenance.id != draft.id,
            )
            .values(status=SUPERSEDED)
        )
        draft.status = APPROVED
        draft.approved_at = func.now()
    else:
        draft.status = REJECTED
        draft.rejected_at = func.now()
    draft.reviewed_by = reviewed_by
    db.flush()
    log.info(
        "agent draft decided (correlation_id=%s version=%s approved=%s by_user_id=%s)",
        draft.correlation_id, draft.version, approve, reviewed_by,
    )
    ai_metrics.record_review(
        outcome=APPROVED if approve else REJECTED,
        duration_seconds=_seconds_awaiting_review(draft),
    )
    if trace is not None:
        # W10 Final Stage 4 (tightened): who reviewed is already durable in
        # audit_logs (see the caller's own _write_audit call) — this trace
        # persists only the decision category and which version it applies
        # to, never a user id.
        trace.review(
            decision=APPROVED if approve else REJECTED,
            draft_version=draft.version,
        )
    return draft


def approved_draft(db: Session, patient_id: int,
                   trace: Optional[TraceRecorder] = None) -> Optional[AgentDraftProvenance]:
    """The one version a patient may see, or None.

    Default deny, exactly like the review gate: no approved row means nothing is
    shown. Returns the STORED row — the caller displays `generated_text` as-is
    and never regenerates it, because a model response is not reproducible and a
    regeneration could differ from what was approved.
    """
    try:
        draft = db.execute(
            select(AgentDraftProvenance).where(
                AgentDraftProvenance.patient_id == patient_id,
                AgentDraftProvenance.status == APPROVED,
            )
        ).scalars().one_or_none()
        if trace is not None and draft is not None:
            # WHICH version was shown, and under what label. Not what it said —
            # that is the whole boundary adr/0010 draws.
            trace.display(
                draft_version=draft.version,
                label=ProvenanceLabel(draft.provenance_label),
            )
        return draft
    except SQLAlchemyError as exc:
        log.error("approved_draft: database error for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise


def has_pending_draft(db: Session, patient_id: int) -> bool:
    """Whether this patient has a VALIDATED draft still waiting on a
    clinician's approve/reject decision — the same status AgentDraftPanel
    checks to decide whether to show its own decision buttons. Used only to
    tell a patient "waiting for review" versus "nothing requested yet"; it
    never says what the draft contains, since only its status is ever theirs
    to see (`draft`/`refused`/`rejected`/`superseded` all read as "none" to
    a patient — none of them is a version they may ever see)."""
    try:
        return db.execute(
            select(AgentDraftProvenance.id).where(
                AgentDraftProvenance.patient_id == patient_id,
                AgentDraftProvenance.status == VALIDATED,
            )
        ).scalars().first() is not None
    except SQLAlchemyError as exc:
        log.error("has_pending_draft: database error for patient_id=%s error_type=%s", patient_id, type(exc).__name__)
        raise


def citations_for(db: Session, draft_id: int) -> list:
    return list(
        db.execute(
            select(AgentDraftCitation).where(AgentDraftCitation.draft_id == draft_id)
        ).scalars().all()
    )
