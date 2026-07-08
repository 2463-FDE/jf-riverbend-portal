"""Loads db/seed/goldset.json — the contractor's unverified retrieval gold
set (see docs/planning/gold-set-risk-log-07-08-2026.md, RISK-GS-01). Loading
it here does not validate it; that is exactly what this harness's metrics
are for.
"""
import json
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class GoldCase:
    query: str
    expected_patient_id: int
    expected_answer: str
    cites_records: List[int]


def load_goldset(path: str) -> List[GoldCase]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return [
        GoldCase(
            query=case["query"],
            expected_patient_id=case["expected_patient_id"],
            expected_answer=case["expected_answer"],
            cites_records=case["cites_records"],
        )
        for case in data["cases"]
    ]
