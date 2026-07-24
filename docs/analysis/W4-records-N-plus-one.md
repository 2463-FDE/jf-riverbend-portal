# Week 4 — Records N+1 Note

Repository snapshot: `main` @ `c43a5f1`, 2026-07-23. Documentation only — no
query, index, model, or service code is changed by this document.

## 1. Current `1 + N` path (existing, unchanged)

`services/records-service/app.py:86-134`, `get_patient_records`, marked
`DEBT D8` in its own docstring:

```python
encounters = (
    db.execute(
        select(Encounter)
        .where(Encounter.patient_id == patient_id)
        .order_by(Encounter.id)
    )
    .scalars()
    .all()
)

chart: list[EncounterWithRecords] = []
# N+1: one extra query per encounter (deliberate — do not collapse to a join)
for enc in encounters:
    recs = (
        db.execute(
            select(Record)
            .where(Record.encounter_id == enc.id)
            .order_by(Record.id)
        )
        .scalars()
        .all()
    )
    chart.append(
        EncounterWithRecords(
            encounter=EncounterOut.model_validate(enc),
            records=[RecordOut.model_validate(r) for r in recs],
        )
    )
```

Query count for one call to `GET /patients/{patient_id}/records`:

```
1 query  — select encounters where patient_id = :id
N queries — one select per encounter, where encounter_id = :enc.id
---------
1 + N total, N = number of encounters for that patient
```

This is a deliberate, explicitly-commented piece of debt (`"deliberate — do
not collapse to a join"`) — not an oversight the comment is warning a future
editor away from casually removing without understanding why it's there
(likely: it was left as an obvious, demonstrable performance defect for the
incoming team to find and fix, consistent with `ARCHITECTURE.md` §7 listing
"N+1 + full-table scans in the records read/search paths" as known,
intentional debt).

A related but separate defect in the same file, **not** addressed by this
note: `search_records` (`services/records-service/app.py:137-159`) does an
unindexed `ILIKE` full-table scan with no result limit. This is explicitly
out of scope for Week 4's graph work — the Week 4 plan says not to expose it
as an agent tool at all, and this note does not propose fixing it.

## 2. Proposed joined/eager read model (design only — not implemented here)

For the **existing production endpoint**, the standard fix (not applied in
this document) would be a single query using SQLAlchemy eager loading, e.g.:

```python
encounters = (
    db.execute(
        select(Encounter)
        .where(Encounter.patient_id == patient_id)
        .options(selectinload(Encounter.records))
        .order_by(Encounter.id)
    )
    .scalars()
    .all()
)
```

`selectinload` would still issue 2 queries total (one for encounters, one
`WHERE encounter_id IN (...)` for all their records) rather than 1 — this is
the standard, well-understood SQLAlchemy tradeoff (avoids a fan-out JOIN's
row duplication) and would already be a fixed, low constant instead of
`1 + N`. A single-query `JOIN` (encounters LEFT JOIN records) is also
possible but returns duplicated encounter columns once per record row, which
the current `EncounterWithRecords` / `RecordOut` Pydantic shape does not
already handle — that reshaping is implementation work, not a documentation
decision, and is intentionally not done here.

**For Week 4's graph reader specifically** (Stage 2, not this document): the
plan calls for "a read-only repository adapter or fixture adapter that groups
a single joined/eager result set into encounters and records without calling
the existing N+1 endpoint." Concretely, that means Stage 2's repository
adapter should either:

- run one `selectinload`-based query (or equivalent single/2-query fixture
  read) directly against seed-derived rows, then group in Python; or
- read pre-joined fixture rows (e.g., a flat list of `(encounter, record)`
  tuples) and group them in memory with no per-encounter query at all.

Either way, the **existing** `get_patient_records` endpoint is not touched,
not imported, and not called by the Stage 2/3 code — the graph reader is a
parallel, independent read path built for the prototype, not a wrapper around
the N+1 endpoint.

## 3. Query-count validation plan (to be executed in Stage 2, not here)

Once the repository adapter exists, a unit test should assert an upper bound
on query/read count for a fixed seeded patient scope — for example (shape
only; exact fixture/counter mechanism is a Stage 2 decision):

```python
def test_repository_adapter_reads_bounded_query_count(seeded_patient_scope):
    with count_reads() as counter:
        chart = repository.load_chart(seeded_patient_scope)
    assert counter.count <= 2  # not proportional to len(chart.encounters)
```

The test should specifically assert the count does **not** grow when the
number of seeded encounters grows (i.e., assert a constant, not just "some
number ≤ current N+1"), so a regression back to per-encounter reads would
fail the test even if the fixed constant is 2 or 3 rather than 1.

## 4. Explicit non-goals

- This note does not modify `services/records-service/app.py`. The existing
  `/patients/{patient_id}/records` endpoint's `1 + N` behavior is unchanged
  and remains documented debt (`DEBT D8`, `ARCHITECTURE.md` §7).
- This note does not add a database index, migration, or `providers` foreign
  key.
- This note does not address `search_records`'s unindexed full-table scan —
  out of scope for Week 4, and that endpoint must not be exposed as an agent
  tool per the Week 4 plan's security considerations.
- This note does not claim the proposed read model has been benchmarked
  against real data volume — the seeded sample is small by design (see
  `docs/planning/W4-patient-knowledge-graph.md` §6), so any Stage 2
  query-count test proves bounded query *count*, not production-scale
  latency.
- Query-count reduction is a performance/scalability improvement for the new
  prototype read path only. It has no bearing on RIV-201 — a faster N+1-free
  query still returns the wrong (unauthorized) patient's data if the
  authorization check documented in
  `docs/analysis/RIV-201-patient-records-IDOR.md` is skipped.
