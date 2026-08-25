# HL7 v2 segment comprehension — Week 6 (2026-08-25)

**Scope:** `services/interop-service`'s inbound hospital-feed parser
(`hl7_parser.py`, called from `app.py::ingest`). Documentation only — no
parsing behavior changes in this pass; see `adr/0011` for the proposed future
mapping boundary.

## Per-segment status

| Segment | Meaning | Mapped today? | Internal record field | Evidence |
|---|---|---|---|---|
| `PID` | Patient demographics | Yes | `mrn`, `name`, `dob` | `hl7_parser.py:12`, `tests/test_hl7_parser.py::test_parses_patient_name_and_dob` |
| `PV1` | Visit | Yes | `provider`, `location` | `hl7_parser.py:13`, `tests/test_hl7_parser.py::test_parses_visit_provider_and_location` |
| `AL1` | Allergy | **No — silently dropped** | `allergies` (schema field exists, always empty) | `hl7_parser.py:11` (`SEGMENT_MAP` has no `AL1` key); `tests/test_hl7_parser.py::test_allergies_and_medications_are_captured` (strict `xfail`) |
| `RXA` | Medication administration | **No — silently dropped** | `medications` (schema field exists, always empty) | same as above |
| any other/malformed segment | — | No — skipped without error | none | `tests/test_hl7_parser.py::test_unknown_segments_do_not_crash` |

## How the drop happens

`SEGMENT_MAP` (`hl7_parser.py:11-14`) only has entries for `PID`/`PV1`. Every
other segment type hits a `KeyError` inside `parse()`'s `try` block, which is
caught by a bare `except Exception: pass` — the same code path a genuinely
malformed line takes. There is no distinct "segment type recognized but not
mapped" signal versus "segment unreadable"; both look identical from the
caller's side.

`app.py::ingest` returns a static `UNMAPPED_NOTE` string
("Only PID and PV1 segments are mapped...") with every response — this is an
existing, truthful disclosure at the API boundary, not new in this pass. It is
not computed from the actual message, so it cannot report per-message which
segments a specific inbound message happened to carry.

## What this is not

This is a comprehension report, not a fix. The parser's behavior is
unchanged: `AL1`/`RXA` are still dropped, `ParsedRecord.allergies`/
`.medications` are still always empty lists for any message ingested through
this path, and the existing strict `xfail`
(`test_allergies_and_medications_are_captured`) still documents the gap
rather than passing. Per `CLAUDE.md`'s Known Risks / Debt: *"HL7 mapping only
handles PID/PV1; allergy (AL1) and medication (RXA) segments are silently
dropped. Still open."* — unchanged by this pass.

## Related

- `adr/0004-master-patient-index-match-key.md` — names this same gap
  (`AUD-04`) as compounding the duplicate-patient problem: an HL7-sourced
  chart silently missing allergy data is indistinguishable from a chart with
  a genuine "no known allergies" fact.
- `adr/0011-hl7-segment-mapping-schema-boundary.md` — proposed (not
  implemented) schema-validated mapping boundary for a future pass.
- `tests/test_hl7_parser.py` — characterization tests for current behavior
  plus the strict expected failure for the desired future behavior.
