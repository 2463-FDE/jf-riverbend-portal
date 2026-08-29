"""db/policy_retrieval_gate.py — the deterministic CI gate promoting the
approved 28-case client gold set into ordinary PR CI (W10 Final Stage 5,
sub-slice 4). No real Postgres, no paid provider: KeywordPolicyRetriever
only. db/policy_corpus_evaluate.py's credentialed real-provider comparison
is a separate, manually-run command, never exercised here.
"""
from conftest import load_module

gate = load_module("db/policy_retrieval_gate.py", "policy_retrieval_gate_mod")


def test_the_gate_passes_against_the_current_corpus_and_gold_set():
    assert gate.main() == 0


def test_a_tampered_alias_file_is_caught_as_drift(tmp_path, monkeypatch):
    tampered = tmp_path / "citation-aliases.json"
    tampered.write_text('{"schema_version": 1, "aliases": {}}', encoding="utf-8")
    monkeypatch.setattr(gate, "_ALIASES", str(tampered))

    problems = gate._check_artifact_drift()

    assert any("citation-aliases.json drifted" in p for p in problems)


def test_a_tampered_gold_set_file_is_caught_as_drift(tmp_path, monkeypatch):
    tampered = tmp_path / "retrieval-evaluations.jsonl"
    tampered.write_text("", encoding="utf-8")
    monkeypatch.setattr(gate, "_EVALUATIONS", str(tampered))

    problems = gate._check_artifact_drift()

    assert any("retrieval-evaluations.jsonl drifted" in p for p in problems)


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
