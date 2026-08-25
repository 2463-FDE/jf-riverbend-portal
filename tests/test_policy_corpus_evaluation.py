"""Client-case mapping and sanitized retrieval scoring."""
import json

import pytest

from libs.policy_corpus import RetrievalScope, RetrievedChunk
from libs.policy_corpus.evaluation import (
    AliasTarget,
    EvaluationCase,
    EvaluationContractError,
    classify_case,
    evaluate_retrieval,
    load_aliases,
    load_case_overrides,
    load_evaluation_cases,
)
from libs.policy_corpus.manifest import load_manifest
from libs.policy_navigator import scope_for_role

MANIFEST_PATH = "docs/RagDocs/manifest.json"
ALIASES_PATH = "docs/client-inputs/2026-08-24/evaluations/citation-aliases.json"
EVALUATIONS_PATH = "docs/client-inputs/2026-08-24/evaluations/retrieval-evaluations.jsonl"


def _chunk(source_id="EDU-A1C-001", version="1.1"):
    return RetrievedChunk(
        citation_id=f"{source_id}@{version}#overview",
        source_id=source_id,
        source_version=version,
        title="Synthetic policy",
        effective_date="2026-08-24",
        section_id="overview",
        heading_path=("Overview",),
        score=0.9,
        text="ephemeral evidence",
    )


class _Retriever:
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    def retrieve(self, question, scope, limit):
        self.calls.append((question, scope, limit))
        return list(self.chunks)


def _case(**overrides):
    fields = dict(
        eval_id="T1",
        actor_roles=("patient",),
        expected_behavior="allowed",
        question="synthetic secret question",
        required_citation_ids=("EDU-DER-A1C-LIMITS@2026-08-22",),
        forbidden_citation_ids=(),
        refusal_or_escalation=False,
    )
    fields.update(overrides)
    return EvaluationCase(**fields)


def test_real_client_suite_is_mapped_without_claiming_disabled_sources_pass():
    manifest = load_manifest(MANIFEST_PATH)
    aliases = load_aliases(ALIASES_PATH)
    overrides = load_case_overrides(ALIASES_PATH)
    cases = load_evaluation_cases(EVALUATIONS_PATH)
    classifications = {case.eval_id: classify_case(case, aliases, manifest, overrides) for case in cases}

    assert len(cases) == 28
    assert classifications["E01"].required_targets == ("EDU-A1C-001@1.1",)
    assert classifications["E19"].classification == "deferred"
    assert classifications["E08"].classification == "negative"
    assert classifications["E07"].required_targets == ("LAB-REL-EXCEPTION-001@1.0",)
    assert classifications["E09"].classification == "spec_conflict"
    assert classifications["E10"].classification == "spec_conflict"
    assert classifications["E25"].classification == "spec_conflict"


def test_unknown_client_citation_is_rejected_instead_of_guessed():
    manifest = load_manifest(MANIFEST_PATH)

    with pytest.raises(EvaluationContractError, match="no citation mapping"):
        classify_case(_case(required_citation_ids=("UNKNOWN@1.0",)), {}, manifest)


def test_application_role_scope_is_used_instead_of_client_categories():
    manifest = load_manifest(MANIFEST_PATH)
    aliases = load_aliases(ALIASES_PATH)
    retriever = _Retriever([])
    case = _case(actor_roles=("front_desk",))

    evaluate_retrieval(
        [case], aliases=aliases, manifest=manifest, retriever=retriever,
        scope_resolver=scope_for_role, top_k=5,
    )

    _, scope, _ = retriever.calls[0]
    assert scope == scope_for_role("front_desk")
    assert "patient_summary" not in scope.workflows


def test_required_forbidden_and_unauthorized_results_are_scored():
    manifest = load_manifest(MANIFEST_PATH)
    aliases = load_aliases(ALIASES_PATH)
    required = _case()
    negative = _case(
        eval_id="T2", expected_behavior="unauthorized", required_citation_ids=(),
        forbidden_citation_ids=("UNAPP-900@2026-08-22",), refusal_or_escalation=True,
    )
    retriever = _Retriever([_chunk(), _chunk("UNAPP-900", "2026-08-22")])

    report = evaluate_retrieval(
        [required, negative], aliases=aliases, manifest=manifest, retriever=retriever,
        scope_resolver=lambda role: RetrievalScope(("patient",), ("patient_summary",)), top_k=5,
    )

    assert report.required_hits == 1
    assert report.forbidden_citation_count == 1
    assert report.unauthorized_retrieval_count == 2  # unapproved chunk appears in both calls
    assert not report.results[1].retrieval_passed


def test_report_never_contains_question_or_retrieved_text():
    manifest = load_manifest(MANIFEST_PATH)
    aliases = load_aliases(ALIASES_PATH)
    retriever = _Retriever([_chunk()])

    report = evaluate_retrieval(
        [_case()], aliases=aliases, manifest=manifest, retriever=retriever,
        scope_resolver=scope_for_role, top_k=5,
    )
    rendered = json.dumps(report.as_dict())

    assert "synthetic secret question" not in rendered
    assert "ephemeral evidence" not in rendered
    assert report.as_dict()["agent_refusal_accuracy"] is None


def test_deferred_case_is_visible_but_never_counted_as_a_pass():
    manifest = load_manifest(MANIFEST_PATH)
    aliases = {"CIT@1.0": AliasTarget("excluded", None, None, "citation_only")}
    case = _case(required_citation_ids=("CIT@1.0",))
    retriever = _Retriever([_chunk()])

    report = evaluate_retrieval(
        [case], aliases=aliases, manifest=manifest, retriever=retriever,
        scope_resolver=scope_for_role, top_k=5,
    )

    assert report.deferred_cases == 1
    assert report.case_coverage == 0
    assert not report.results[0].retrieval_passed
    assert retriever.calls == []
