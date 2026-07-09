"""Heuristic identity-cluster proxy used ONLY to measure fragmentation in the
eval corpus (duplicate-rate, fragment-coverage gap). This is NOT a
production match-key and does not fix AUD-09 — it exists so this harness can
report how much AUD-09 affects retrieval quality, per the metric
definitions in `.claude/skills/w2-deliverable-planner/SKILL.md`. A
production match-key approach is proposed only, via
`adr/0004-master-patient-index-match-key.md` — no matching logic from that
proposal is implemented here or anywhere else in this commit.

Proxy method: normalized SSN match across `db/seed/patients.csv` rows. SSN
is the one field identical across all three Maria Gonzalez fixture rows
(1042/1330/1588) despite differing name spellings and, on one row, a
differing DOB — a deliberately messy fixture (see
`db/seed/generate_seed.py`). Real intake data cannot be assumed to have a
reliable, non-blank SSN for every patient; this proxy is a measurement
convenience for this harness's small seed corpus, not a general-purpose
matching algorithm, and this limitation is called out in the generated
eval report rather than hidden.
"""
import csv
import os
from collections import defaultdict
from typing import Dict, List

_PATIENTS_CSV = os.path.join(os.path.dirname(__file__), "..", "..", "db", "seed", "patients.csv")


def _normalize_ssn(ssn: str) -> str:
    return "".join(ch for ch in ssn if ch.isdigit())


def cluster_patients(path: str = _PATIENTS_CSV) -> Dict[int, List[int]]:
    """Returns {patient_id: [all patient_ids sharing that patient's normalized SSN]}."""
    by_ssn: Dict[str, List[int]] = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            by_ssn[_normalize_ssn(row["ssn"])].append(int(row["id"]))

    clusters: Dict[int, List[int]] = {}
    for ids in by_ssn.values():
        for patient_id in ids:
            clusters[patient_id] = ids
    return clusters
