"""Stage 3 composer — the only place in this package that may call a model,
and only to phrase a plain-language summary of evidence the evidence
validator has ALREADY approved.

It never decides authorization or escalation, and it never receives
anything the supervisor hasn't already validated — there is no patient_id,
tool name, URL, or SQL surface here for a model to influence. The prompt
built in `_build_prompt` carries only counts, evidence ids, and coarse node
type/status metadata; none of `ChartResult`/`PatientGraph`/`GraphNode` has a
record-body or free-text-narrative field to leak in the first place (see
contracts.py).

Reuses Week 3's bounded-turn / fake-provider / safe-error pattern
(libs/eligibility_agent/runtimes/raw_bedrock.py) at a much smaller scale: a
single bounded `LLMClient.complete()` call per attempt, structured-output
validated, capped at `max_attempts`, with a fully deterministic template
fallback on a provider error OR a response that cites an evidence id outside
the already-validated set. `llm_client=None` (the default used by
`runtime.run_patient_view` and `demo.py`) skips the model entirely and
returns the template directly — recounting already-validated structured
evidence in plain language does not require a live model for this bounded
prototype. Even when an `LLMClient` is supplied, the default provider is
`fake` (`LLM_PROVIDER=fake`, returning the literal text `"{}"`), which always
fails schema validation, so the template path is what actually runs unless a
test explicitly scripts a `FakeProvider` — no live provider or network is
ever contacted by this module.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from libs.llm_client.client import LLMClient
from libs.llm_client.errors import LLMClientError
from libs.safe_logging import get_safe_logger

from .contracts import AuthorizedScope
from .specialists import ValidatedEvidence

log = get_safe_logger(__name__)

_DEFAULT_MAX_ATTEMPTS = 2


class ComposedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    cited_evidence_ids: list[str] = []


def _template_summary(scope: AuthorizedScope, evidence: ValidatedEvidence) -> ComposedSummary:
    if evidence.no_evidence:
        text = f"No encounters or records were found for patient {scope.patient_id} in this seeded view."
    else:
        n_enc = len(evidence.chart.encounters)
        n_rec = len(evidence.chart.records)
        text = (
            f"Patient {scope.patient_id}'s seeded chart has {n_enc} encounter(s) and "
            f"{n_rec} record(s). See the cited evidence ids for the specific entries."
        )
    # Always cites exactly the validator-approved set — never a superset —
    # so the template path can never itself be a source of unsupported
    # evidence, regardless of what the (skipped, in this branch) model would
    # have said.
    return ComposedSummary(summary=text, cited_evidence_ids=list(evidence.evidence_ids))


def _build_prompt(evidence: ValidatedEvidence, retry_note: str = "") -> str:
    lines = [f"encounters={len(evidence.chart.encounters)}", f"records={len(evidence.chart.records)}"]
    for node in evidence.graph.nodes:
        lines.append(f"{node.node_id}:{node.node_type.value}")
    instructions = (
        "Summarize this patient's seeded chart in one or two plain-language sentences "
        "using ONLY the evidence ids listed below. Return JSON with fields 'summary' "
        "and 'cited_evidence_ids' (a subset of the listed ids). Never invent an id."
    )
    return instructions + retry_note + "\n" + "\n".join(lines)


def compose(
    scope: AuthorizedScope,
    evidence: ValidatedEvidence,
    *,
    llm_client: Optional[LLMClient] = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> tuple[ComposedSummary, int, bool]:
    """Returns `(summary, attempts, used_fallback)`.

    `llm_client=None` skips the model path entirely: 0 attempts, the
    deterministic template, `used_fallback=False` (there was nothing to fall
    back FROM). With an `llm_client`, at most `max_attempts` bounded calls are
    made (a plain `for` loop, mirroring `RawBedrockAgentRuntime`'s
    structurally-guaranteed termination) before giving up and using the
    template — this never raises and never loops unbounded.
    """
    if llm_client is None:
        return _template_summary(scope, evidence), 0, False

    validated_ids = set(evidence.evidence_ids)
    retry_note = ""
    attempts = 0

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            result = llm_client.complete(_build_prompt(evidence, retry_note), schema=ComposedSummary)
        except LLMClientError as exc:
            log.warning("patient_view compose provider error (attempt=%s, error_type=%s)", attempt, type(exc).__name__)
            return _template_summary(scope, evidence), attempts, True

        if set(result.cited_evidence_ids) <= validated_ids:
            return result, attempts, False

        log.warning("patient_view compose rejected (attempt=%s, reason=unsupported_citation)", attempt)
        retry_note = "\nYour previous answer cited an id not in the list below. Use ONLY the listed ids."

    return _template_summary(scope, evidence), attempts, True
