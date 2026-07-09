"""Metric definitions and computation for the retrieval-eval harness. See
`.claude/skills/w2-deliverable-planner/SKILL.md` for the exact definitions
this implements:

- recall@k: % of gold-set questions where at least one expected relevant
  record appears in the top-k retrieved records.
- precision@k: % of top-k retrieved records that are relevant according to
  the gold set.
- duplicate-rate: % of patient/person entities represented by more than one
  patient/chart fragment in the eval corpus.
- fragment-coverage gap: % of gold-set questions where the correct answer
  exists in the corpus but is attached to a different patient/chart
  fragment than the retrieved fragment.
"""
from dataclasses import dataclass
from typing import Dict, List

from libs.rag_corpus import CorpusRecord

from .clinical_fields import has_relevant_clinical_content, infer_clinical_domain
from .goldset import GoldCase


@dataclass
class EvalReport:
    provider_name: str
    top_k: int
    total_cases: int
    recall_at_k: float
    precision_at_k: float
    duplicate_rate: float
    fragment_coverage_gap: float
    per_case: List[dict]
    duplicate_clusters: List[List[int]]


def _cites_to_record_ids(cites_records: List[int]) -> List[str]:
    # Positional mapping only: db/seed/encounters.csv row N (1-indexed)
    # happens to correspond to the same fixture position as the original
    # `records` table id N in db/seed/generate_seed.py, since both are
    # generated from the same ordered fixture list. encounters.csv has no
    # native record-id column, so this is a harness-level convenience, not a
    # real foreign key — flagged as a known limitation in the generated
    # report rather than hidden.
    return [f"seed-enc-{n:04d}" for n in cites_records]


def compute_metrics(
    gold_cases: List[GoldCase],
    corpus: List[CorpusRecord],
    retrieved_by_case: Dict[str, List[CorpusRecord]],
    clusters: Dict[int, List[int]],
    top_k: int,
    provider_name: str,
) -> EvalReport:
    corpus_patient_ids = {record.patient_id for record in corpus}

    recall_hits = 0
    precision_scores: List[float] = []
    gap_hits = 0
    per_case = []

    for case in gold_cases:
        expected_record_ids = set(_cites_to_record_ids(case.cites_records))
        retrieved = retrieved_by_case[case.query]
        retrieved_ids = [record.record_id for record in retrieved]
        retrieved_patient_ids = {record.patient_id for record in retrieved}

        hit = bool(expected_record_ids & set(retrieved_ids))
        recall_hits += int(hit)

        relevant_in_topk = len(expected_record_ids & set(retrieved_ids))
        precision_scores.append(relevant_in_topk / len(retrieved) if retrieved else 0.0)

        cluster = clusters.get(case.expected_patient_id, [case.expected_patient_id])
        sibling_ids_in_corpus = [
            patient_id
            for patient_id in cluster
            if patient_id != case.expected_patient_id and patient_id in corpus_patient_ids
        ]
        clinical_domain = infer_clinical_domain(case.query, case.expected_answer)
        siblings_with_relevant_content = [
            patient_id
            for patient_id in sibling_ids_in_corpus
            if has_relevant_clinical_content(patient_id, clinical_domain)
        ]
        gap = bool(siblings_with_relevant_content) and not (
            set(siblings_with_relevant_content) & retrieved_patient_ids
        )
        gap_hits += int(gap)

        per_case.append(
            {
                "query": case.query,
                "expected_patient_id": case.expected_patient_id,
                "expected_record_ids": sorted(expected_record_ids),
                "retrieved_record_ids": retrieved_ids,
                "recall_hit": hit,
                "fragment_coverage_gap": gap,
                "clinical_domain": clinical_domain,
                "cluster_patient_ids": cluster,
            }
        )

    total = len(gold_cases)
    distinct_clusters = {tuple(sorted(ids)) for ids in clusters.values()}
    duplicate_clusters = sorted(cluster for cluster in distinct_clusters if len(cluster) > 1)

    return EvalReport(
        provider_name=provider_name,
        top_k=top_k,
        total_cases=total,
        recall_at_k=100.0 * recall_hits / total if total else 0.0,
        precision_at_k=100.0 * sum(precision_scores) / total if total else 0.0,
        duplicate_rate=(100.0 * len(duplicate_clusters) / len(distinct_clusters)) if distinct_clusters else 0.0,
        fragment_coverage_gap=100.0 * gap_hits / total if total else 0.0,
        per_case=per_case,
        duplicate_clusters=[list(cluster) for cluster in duplicate_clusters],
    )
