"""db/policy_retrieval_gate.py — the deterministic CI gate promoting the
approved 28-case client gold set into ordinary PR CI (W10 Final Stage 5,
sub-slice 4). No real Postgres, no paid provider: KeywordPolicyRetriever
only. db/policy_corpus_evaluate.py's credentialed real-provider comparison
is a separate, manually-run command, never exercised here.
"""
import hashlib

import pytest

from conftest import load_module

gate = load_module("db/policy_retrieval_gate.py", "policy_retrieval_gate_mod")


def test_the_gate_passes_against_the_current_corpus_and_gold_set():
    assert gate.main() == 0


# --- GATE-PROVENANCE-BYPASS: a coordinated edit to an artifact AND its own
# provenance document must still fail against the pinned code baseline -----


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("relative_path,artifact_attr,provenance_attr,provenance_line_fmt", [
    (
        "evaluations/retrieval-evaluations.jsonl", "_EVALUATIONS", "_SHA256SUMS",
        "{digest}  evaluations/retrieval-evaluations.jsonl\n",
    ),
    (
        "evaluations/citation-aliases.json", "_ALIASES", "_ADOPTION_NOTES",
        "sha256: {digest}  evaluations/citation-aliases.json\n",
    ),
])
def test_a_coordinated_edit_to_artifact_and_its_own_provenance_still_fails(
    tmp_path, monkeypatch, relative_path, artifact_attr, provenance_attr, provenance_line_fmt,
):
    """The bug this fixes: the OLD check only ever compared an artifact's
    real hash against whatever hash its OWN provenance document recorded —
    so editing both consistently (change the content, update the recorded
    hash to match) passed silently. Constructing exactly that scenario
    here proves it: the tampered artifact and its tampered provenance
    record agree with EACH OTHER (the old check's only comparison) but
    neither matches the pinned `_APPROVED_ARTIFACT_HASHES` baseline, so
    the corrected check must still report both mismatches."""
    tampered_content = "tampered content unrelated to the approved artifact"
    tampered_digest = _sha256(tampered_content)
    assert tampered_digest != gate._APPROVED_ARTIFACT_HASHES[relative_path]

    artifact_path = tmp_path / "artifact"
    artifact_path.write_text(tampered_content, encoding="utf-8")
    provenance_path = tmp_path / "provenance"
    provenance_path.write_text(provenance_line_fmt.format(digest=tampered_digest), encoding="utf-8")

    monkeypatch.setattr(gate, artifact_attr, str(artifact_path))
    monkeypatch.setattr(gate, provenance_attr, str(provenance_path))

    # The scenario the old, vulnerable check would have accepted: artifact
    # and provenance record agree with each other.
    assert gate._sha256(str(artifact_path)) == tampered_digest

    problems = gate._check_artifact_drift()

    assert len(problems) == 2, problems  # artifact mismatch AND provenance mismatch, both reported
    assert any("artifact hash" in p and relative_path in p for p in problems)
    assert any("provenance-recorded hash" in p and relative_path in p for p in problems)
    # Never the tampered content itself, only hashes and paths.
    assert not any(tampered_content in p for p in problems)


def test_a_metric_below_its_floor_is_reported_by_label():
    class _Report:
        recall_at_k = 0.5
        citation_target_accuracy = 1.0
        case_coverage = 0.9
        forbidden_citation_count = 0
        unauthorized_retrieval_count = 0

    failures = gate._threshold_failures("test_contract", _Report(), {"recall_at_k": 0.9})

    assert len(failures) == 1
    assert "test_contract" in failures[0] and "recall_at_k" in failures[0]


def test_a_forbidden_or_unauthorized_hit_always_fails_regardless_of_thresholds():
    class _Report:
        recall_at_k = 1.0
        citation_target_accuracy = 1.0
        case_coverage = 1.0
        forbidden_citation_count = 1
        unauthorized_retrieval_count = 2

    failures = gate._threshold_failures("test_contract", _Report(), {})

    assert len(failures) == 2


def test_patient_summary_scope_ignores_the_gold_sets_own_actor_role():
    """Mirrors summary_agent_path.generate_draft exactly: the summary's own
    audience is always "patient", never derived from who is asking."""
    scope = gate._patient_summary_scope("clinician")

    assert scope.audiences == ("patient",)
    assert scope.workflows == ("patient_summary",)
