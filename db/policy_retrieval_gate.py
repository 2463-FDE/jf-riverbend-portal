#!/usr/bin/env python3
"""Deterministic retrieval-quality CI gate (W10 Final Stage 5, sub-slice 4).

Runs the approved 28-case client gold set through libs.policy_corpus's own
evaluation harness against `KeywordPolicyRetriever` — a deterministic,
no-network baseline (never real Bedrock/pgvector; that credentialed
real-provider comparison stays db/policy_corpus_evaluate.py's job, run
manually/periodically, never in ordinary PR CI). Covers both the
policy-navigator retrieval contract (scope_for_role, all cases) and the
patient-summary retrieval contract (the fixed patient/patient_summary scope
services/records-service/summary_agent_path.py actually authorizes,
evaluated against this gold set's patient-role cases). Also fails if the
gold set or its alias/override file has drifted from the provenance hash
recorded for each in docs/client-inputs/2026-08-24/{SHA256SUMS.txt,
ADOPTION-NOTES.md} — evidence of an edit nobody re-verified against the
client package.

Thresholds below are the measured baseline for the current corpus/gold set,
not aspirational — a value regressing below its recorded floor means a real
retrieval or authorization regression, not a flaky comparison: this whole
gate is exact-input deterministic (fixed manifest, fixed gold set, fixed
keyword ranking), so any drop is a code or data change, not noise.
"""
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from libs.policy_corpus import RetrievalScope  # noqa: E402
from libs.policy_corpus.evaluation import (  # noqa: E402
    EvaluationReport,
    KeywordPolicyRetriever,
    evaluate_retrieval,
    load_aliases,
    load_case_overrides,
    load_evaluation_cases,
)
from libs.policy_corpus.manifest import load_manifest  # noqa: E402
from libs.policy_navigator import scope_for_role  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CLIENT_INPUT_DIR = os.path.join(_ROOT, "docs", "client-inputs", "2026-08-24")
_MANIFEST = os.path.join(_ROOT, "docs", "RagDocs", "manifest.json")
_EVALUATIONS = os.path.join(_CLIENT_INPUT_DIR, "evaluations", "retrieval-evaluations.jsonl")
_ALIASES = os.path.join(_CLIENT_INPUT_DIR, "evaluations", "citation-aliases.json")
_SHA256SUMS = os.path.join(_CLIENT_INPUT_DIR, "SHA256SUMS.txt")
_ADOPTION_NOTES = os.path.join(_CLIENT_INPUT_DIR, "ADOPTION-NOTES.md")

# Measured against the current docs/RagDocs corpus + the 28-case gold set —
# see this module's own docstring for why a drop here means a regression.
_NAV_THRESHOLDS = {"recall_at_k": 1.0, "citation_target_accuracy": 1.0, "case_coverage": 0.80}
_PATIENT_SUMMARY_THRESHOLDS = {"recall_at_k": 1.0, "citation_target_accuracy": 1.0, "case_coverage": 0.80}


def _sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _recorded_hash_in_sha256sums(relative_path: str) -> str:
    with open(_SHA256SUMS, encoding="utf-8") as fh:
        for line in fh:
            digest, _, name = line.strip().partition("  ")
            if name == relative_path:
                return digest
    raise SystemExit(f"policy_retrieval_gate: no recorded hash for {relative_path!r} in SHA256SUMS.txt")


def _recorded_hash_in_adoption_notes(basename: str) -> str:
    with open(_ADOPTION_NOTES, encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(rf"sha256:\s*([0-9a-f]{{64}})\s+evaluations/{re.escape(basename)}", text)
    if not match:
        raise SystemExit(f"policy_retrieval_gate: no recorded hash for {basename!r} in ADOPTION-NOTES.md")
    return match.group(1)


def _check_artifact_drift() -> list:
    """Every mismatch found, never raised individually — the caller reports
    all of them together rather than stopping at the first."""
    problems = []
    actual = _sha256(_EVALUATIONS)
    recorded = _recorded_hash_in_sha256sums("evaluations/retrieval-evaluations.jsonl")
    if actual != recorded:
        problems.append(f"retrieval-evaluations.jsonl drifted from SHA256SUMS.txt ({actual} != {recorded})")

    actual = _sha256(_ALIASES)
    recorded = _recorded_hash_in_adoption_notes("citation-aliases.json")
    if actual != recorded:
        problems.append(f"citation-aliases.json drifted from ADOPTION-NOTES.md ({actual} != {recorded})")
    return problems


def _threshold_failures(label: str, report: EvaluationReport, thresholds: dict) -> list:
    failures = []
    for metric, floor in thresholds.items():
        value = getattr(report, metric)
        if value is None or value < floor:
            failures.append(f"{label}: {metric}={value} below required floor {floor}")
    if report.forbidden_citation_count:
        failures.append(f"{label}: forbidden_citation_count={report.forbidden_citation_count} (must be 0)")
    if report.unauthorized_retrieval_count:
        failures.append(f"{label}: unauthorized_retrieval_count={report.unauthorized_retrieval_count} (must be 0)")
    return failures


def _patient_summary_scope(_role: str) -> RetrievalScope:
    # Mirrors services/records-service/summary_agent_path.py::generate_draft
    # exactly: the summary's own audience is always "patient" regardless of
    # who generated it, never derived from the gold-set case's actor_role.
    return RetrievalScope(audiences=("patient",), workflows=("patient_summary",))


def main(argv=None) -> int:
    problems = _check_artifact_drift()

    manifest = load_manifest(_MANIFEST)
    aliases = load_aliases(_ALIASES)
    case_overrides = load_case_overrides(_ALIASES)
    cases = load_evaluation_cases(_EVALUATIONS)
    retriever = KeywordPolicyRetriever(_MANIFEST)

    nav_report = evaluate_retrieval(
        cases, aliases=aliases, manifest=manifest, retriever=retriever,
        scope_resolver=scope_for_role, top_k=5, case_overrides=case_overrides,
    )
    problems.extend(_threshold_failures("policy_navigator", nav_report, _NAV_THRESHOLDS))

    patient_cases = [c for c in cases if c.actor_roles[0] == "patient"]
    summary_report = evaluate_retrieval(
        patient_cases, aliases=aliases, manifest=manifest, retriever=retriever,
        scope_resolver=_patient_summary_scope, top_k=5, case_overrides=case_overrides,
    )
    problems.extend(_threshold_failures("patient_summary", summary_report, _PATIENT_SUMMARY_THRESHOLDS))

    print(json.dumps(
        {"policy_navigator": nav_report.as_dict(), "patient_summary": summary_report.as_dict()},
        indent=2, sort_keys=True,
    ))
    if problems:
        print("RETRIEVAL GATE FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("retrieval gate passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
