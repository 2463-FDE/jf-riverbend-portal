"""Stage 3 — real-database ChartRepositoryPort for the patient-view agent.

Replaces Week 4's fixture-only `libs.patient_view_agent.repository.
SeededChartRepository` (hardcoded seed rows) with a repository bound to the
actual `encounters`/`records` tables, for the one new route this wires
(`GET /patients/{id}/view`). It mirrors that fixture's bounded, minimum-
necessary 2-query pattern (see docs/analysis/W4-records-N-plus-one.md):

    read 1: encounters where patient_id == :id
    read 2: records    where encounter_id IN (:enc_ids) AND patient_id == :id

Each query selects only the columns `libs.patient_view_agent.contracts.
EncounterRow`/`RecordRow` model — `records.body` is never named in the
SELECT, so the clinical narrative never leaves Postgres for this path (a
stricter guarantee than just "the response schema omits it").

This does NOT touch or fix `get_patient_records` (`DEBT D11`, the N+1
IDOR-exposed endpoint) below in app.py — that remains exactly as documented
in docs/analysis/RIV-201-patient-records-IDOR.md. This is a new, additive
read path used only by the authenticated-staff-gated `/view` route.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from libs.patient_view_agent.contracts import ChartResult, EncounterRow, GraphLimits, RecordRow
from libs.patient_view_agent.repository import ChartRepositoryPort
from libs.safe_logging import get_safe_logger

from models import Encounter, Record

log = get_safe_logger(__name__)


class SqlChartRepository(ChartRepositoryPort):
    def __init__(self, db: Session, *, limits: GraphLimits | None = None):
        self._db = db
        self._limits = limits or GraphLimits()

    def load_chart(self, patient_id: int, *, correlation_id: str = "") -> ChartResult:
        limits = self._limits
        reads = 0
        truncated = False

        # read 1 — encounters for this patient. Fetch one row past the cap so
        # truncation can be flagged without a separate COUNT query.
        reads += 1
        enc_rows = self._db.execute(
            select(
                Encounter.id,
                Encounter.patient_id,
                Encounter.encounter_type,
                Encounter.provider,
                Encounter.status,
            )
            .where(Encounter.patient_id == patient_id)
            .order_by(Encounter.id)
            .limit(limits.max_encounters + 1)
        ).all()
        if len(enc_rows) > limits.max_encounters:
            enc_rows = enc_rows[: limits.max_encounters]
            truncated = True
        encounters = [
            EncounterRow(
                id=r.id,
                patient_id=r.patient_id,
                encounter_type=r.encounter_type,
                provider=r.provider,
                status=r.status,
            )
            for r in enc_rows
        ]
        encounter_ids = [e.id for e in encounters]

        # read 2 — records for those encounters. `patient_id` is defense in
        # depth (mirrors SeededChartRepository): even a corrupted/duplicated
        # encounter_id cannot pull another patient's record into scope. Runs
        # even when encounter_ids is empty, so `reads` stays constant at 2
        # regardless of chart size — the same invariant the fixture repo
        # documents.
        reads += 1
        rec_rows = self._db.execute(
            select(
                Record.id,
                Record.encounter_id,
                Record.patient_id,
                Record.kind,
                Record.title,
                Record.status,
            )
            .where(Record.encounter_id.in_(encounter_ids), Record.patient_id == patient_id)
            .order_by(Record.id)
            .limit(limits.max_records + 1)
        ).all()
        if len(rec_rows) > limits.max_records:
            rec_rows = rec_rows[: limits.max_records]
            truncated = True
        records = [
            RecordRow(
                id=r.id,
                encounter_id=r.encounter_id,
                patient_id=r.patient_id,
                kind=r.kind,
                title=r.title,
                status=r.status,
            )
            for r in rec_rows
        ]

        result = ChartResult(
            patient_id=patient_id,
            encounters=encounters,
            records=records,
            reads=reads,
            truncated=truncated,
        )
        log.info(
            "patient_view sql repository load_chart (correlation_id=%s, encounters=%s, records=%s, reads=%s, truncated=%s)",
            correlation_id,
            len(encounters),
            len(records),
            reads,
            truncated,
        )
        return result
