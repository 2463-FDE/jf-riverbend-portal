"""Read-only chart repository — a fixture adapter over deterministic,
seed-derived rows. No database, no network, no new infrastructure dependency.

It mirrors the bounded read model from docs/analysis/W4-records-N-plus-one.md
WITHOUT touching the existing N+1 endpoint:

    read 1:  encounters where patient_id == :id
    read 2:  records    where encounter_id IN (:enc_ids)      # single IN query
    then group records under their encounter in Python.

`reads` is incremented once per logical query and is therefore CONSTANT (2) no
matter how many encounters a patient has — the whole point of the fix. This is
the schema-faithful form from the N+1 note: the current `Encounter`/`Record`
models have no SQLAlchemy relationship, so the real query would be an explicit
`select(Record).where(Record.encounter_id.in_([...]))`, NOT
`selectinload(Encounter.records)`. The fixture reproduces that shape.

The adapter also loads MINIMUM-NECESSARY columns only — no record `body` — so
clinical narrative never enters the read model, the graph, or a log.

`load_calls` accumulates across calls so a test can prove a *denied* request
performs ZERO reads (the repository is never invoked).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from libs.safe_logging import get_safe_logger

from .contracts import ChartResult, EncounterRow, GraphLimits, RecordRow

log = get_safe_logger(__name__)


class ChartRepositoryPort(ABC):
    @abstractmethod
    def load_chart(self, patient_id: int, *, correlation_id: str = "") -> ChartResult:
        """Return the bounded, grouped chart for exactly `patient_id`."""
        raise NotImplementedError


class SeededChartRepository(ChartRepositoryPort):
    def __init__(
        self,
        encounters: Iterable[EncounterRow],
        records: Iterable[RecordRow],
        *,
        limits: GraphLimits | None = None,
    ):
        self._encounters = list(encounters)
        self._records = list(records)
        self._limits = limits or GraphLimits()
        # How many times load_chart was invoked over this repo's lifetime.
        # Stays 0 when authorization denies before any read.
        self.load_calls = 0

    def load_chart(self, patient_id: int, *, correlation_id: str = "") -> ChartResult:
        self.load_calls += 1
        reads = 0
        truncated = False

        # read 1 — encounters for this patient, deterministic order.
        reads += 1
        encounters = sorted(
            (e for e in self._encounters if e.patient_id == patient_id),
            key=lambda e: e.id,
        )
        if len(encounters) > self._limits.max_encounters:
            encounters = encounters[: self._limits.max_encounters]
            truncated = True
        encounter_ids = {e.id for e in encounters}

        # read 2 — records for those encounters, scoped to this patient in a
        # single IN-style pass. The `patient_id` predicate is defense in depth:
        # even if an encounter id were duplicated or corrupted, another
        # patient's record cannot enter the result set. Enforcing it HERE (not
        # only at the graph) keeps the repository's promise to return exactly
        # one patient's chart, and prevents a single bad cross-patient row from
        # tripping the graph's fail-closed check and denying an otherwise
        # legitimate request.
        reads += 1
        records = sorted(
            (r for r in self._records if r.encounter_id in encounter_ids and r.patient_id == patient_id),
            key=lambda r: r.id,
        )
        if len(records) > self._limits.max_records:
            records = records[: self._limits.max_records]
            truncated = True

        result = ChartResult(
            patient_id=patient_id,
            encounters=encounters,
            records=records,
            reads=reads,
            truncated=truncated,
        )
        log.info(
            "patient_view repository load_chart (correlation_id=%s, encounters=%s, records=%s, reads=%s, truncated=%s)",
            correlation_id,
            len(encounters),
            len(records),
            reads,
            truncated,
        )
        return result


# --------------------------------------------------------------------------- #
# Deterministic seed-derived sample (subset of db/seed/seed.sql, verbatim ids)
# --------------------------------------------------------------------------- #
# Faithful to db/seed/seed.sql (patients 1042 Maria Gonzalez, 1043 James
# O'Brien, and 1330 — one of the Maria Gonzalez fragmentation duplicates).
# Bodies are intentionally omitted (minimum-necessary). Kept small and
# deterministic for tests and the manual demo; NOT a production data source.
def seed_derived_sample() -> tuple[list[EncounterRow], list[RecordRow]]:
    encounters = [
        EncounterRow(id=1, patient_id=1042, encounter_type="office_visit", provider="Dr. Patel", status="finished"),
        EncounterRow(id=6, patient_id=1042, encounter_type="office_visit", provider="Dr. Grace Kim", status="finished"),
        EncounterRow(id=4, patient_id=1043, encounter_type="office_visit", provider="Dr. Patel", status="finished"),
        EncounterRow(id=7, patient_id=1043, encounter_type="telehealth", provider="Lab Services", status="finished"),
        EncounterRow(id=2, patient_id=1330, encounter_type="office_visit", provider="Dr. Nguyen", status="finished"),
    ]
    records = [
        RecordRow(id=1, encounter_id=1, patient_id=1042, kind="note", title="Visit note", status="final"),
        RecordRow(id=6, encounter_id=6, patient_id=1042, kind="immunization", title="Immunization", status="final"),
        RecordRow(id=4, encounter_id=4, patient_id=1043, kind="note", title="Visit note", status="final"),
        RecordRow(id=7, encounter_id=7, patient_id=1043, kind="immunization", title="Immunization", status="final"),
        RecordRow(id=2, encounter_id=2, patient_id=1330, kind="note", title="Visit note", status="final"),
    ]
    return encounters, records
