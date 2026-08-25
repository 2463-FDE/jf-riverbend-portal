# ADR 0011 — Schema-validated HL7 segment mapping boundary (proposal)

- **Status:** Proposed — not implemented. This ADR includes no parser
  change, no new dependency, and no schema migration. `hl7_parser.py`'s
  behavior (PID/PV1 mapped, AL1/RXA silently dropped) is unchanged.
- **Date:** 2026-08-25
- **Author:** Week 6 comprehension deliverable (W10 delivery-closure
  planner). No internal Riverbend team name exists in this repo to attribute
  it to otherwise (see `CLAUDE.md`, "Unknowns").

## Context

- `services/interop-service/hl7_parser.py`'s `SEGMENT_MAP` only has entries
  for `PID` and `PV1`. Every other recognized-but-unmapped segment type
  (`AL1`, `RXA`, and any future segment the hospital feed starts sending)
  hits the same bare `except Exception: pass` as a genuinely malformed line —
  there is no way to tell, from the caller's side, "this segment type is
  known but intentionally not mapped" apart from "this line could not be
  parsed at all."
- `docs/planning/hl7-segment-comprehension-week6-08-25-2026.md` documents the
  current per-segment status in detail; this ADR proposes the structural fix
  that report recommends investigating, without implementing it.
- `adr/0004-master-patient-index-match-key.md` (`AUD-04`) already names the
  clinical-safety consequence: an HL7-sourced chart missing allergy data is
  indistinguishable from a chart with a genuine "no known allergies" fact.
- `CLAUDE.md`'s Known Risks / Debt lists this as still open, and explicitly
  scopes it as a backlog item, not something to silently patch as a side
  effect of unrelated work — this ADR respects that boundary.

## Decision (proposed)

Propose the following for a future, separately-scoped implementation.
Nothing below is implemented by this ADR or its accompanying commit.

1. **Declare mapped segments as a schema, not a dict.** Replace the bare
   `SEGMENT_MAP` dict with an explicit, versioned schema — one entry per
   segment type, classified into exactly one of four statuses:
   - `mapped` — `PID`, `PV1` today, with their field indices;
   - `recognized_unmapped` — `AL1`, `RXA`: a real segment type this feed
     sends, with a named reason it isn't parsed. This is the data-loss
     gap: something clinically real is dropped.
   - `ignored_standard` — `MSH` (and any other required HL7 header/control
     segment every message carries): expected on every message, carries no
     clinical content this system needs, and must never trigger the
     "unrecognized segment" alert item 3 proposes below. Distinct from
     `recognized_unmapped` precisely because nothing is lost by not mapping
     `MSH`, unlike `AL1`/`RXA`.
   - `unknown` — a segment type not declared in the schema at all. This is
     the only bucket item 3's alert applies to.

   Without this fourth status, a schema that only had `mapped` /
   `recognized_unmapped` / `unknown` would classify `MSH` — present on
   every normal message, per the bundled sample — as `unknown`, and item
   3's "log at higher severity" would fire on every single ingest.
2. **Per-message comprehension result, as a field on the existing
   response.** Replace `app.py`'s hardcoded `UNMAPPED_NOTE` string with a
   per-message summary computed from what the message actually contained
   (see the worked example below), returned as a field on the SAME
   `/hl7/ingest` response `ParsedRecord` already comes back on — no new
   endpoint, emitted event, or persisted table. This is the only
   propagation target this ADR proposes: no consumer currently reads or
   persists `/hl7/ingest`'s response downstream — the nearest candidate
   consumer, `services/records-service/reconciliation.py`, already lists
   the `AL1`/`RXA` gap as a known limitation without reading anything from
   this endpoint — so a persisted or evented marker would have nowhere to
   go today. If a downstream consumer is built later, propagating this
   same summary to it is that consumer's design question, not decided
   here.
3. **Fail loudly on a genuinely `unknown` segment, not just skip it.**
   Distinguish `recognized_unmapped` (skip silently, as today, but
   recorded — `AL1`/`RXA`), `ignored_standard` (skip silently, never
   alerted — `MSH`), and `unknown` (log at a higher severity so a new
   hospital-feed segment type doesn't disappear unnoticed the way
   `AL1`/`RXA` did).
4. **Implementing AL1/RXA mapping itself is a separate, later decision.**
   This ADR proposes making the drop *visible and structured*; it does not
   propose or schedule actually parsing allergy/medication content. That
   remains explicitly deferred pending separate authorization, per
   `CLAUDE.md`'s standing instruction not to opportunistically fix
   documented debt.

## Worked example (if implemented later)

For an inbound message containing `MSH`, `PID`, `PV1`, `AL1`, `RXA`, and one
genuinely unrecognized `ZZZ` segment (the same shape
`tests/test_hl7_parser.py::test_unknown_segments_do_not_crash` exercises
today, plus the bundled sample's real `MSH`/`AL1`/`RXA` lines):

Mapped fields — unchanged from today's behavior, since this ADR does not
implement AL1/RXA parsing:

```json
{"mrn": "M4471", "name": "Gonzalez^Maria", "dob": "19710302",
 "provider": "1234^Nguyen^Anita", "location": "CLINIC^^^RIVERBEND",
 "allergies": [], "medications": []}
```

Segment-comprehension summary — the new field this ADR proposes, returned
alongside the mapped record on the same response:

```json
{
  "mapped": ["PID", "PV1"],
  "recognized_unmapped": [
    {"segment": "AL1", "reason": "allergy_content_not_parsed"},
    {"segment": "RXA", "reason": "medication_content_not_parsed"}
  ],
  "ignored_standard": ["MSH"],
  "unknown": ["ZZZ"]
}
```

Acceptance criteria for a future implementation:

- every segment type present in an inbound message appears in exactly one
  of the four lists, with no segment type ever appearing in more than one;
- `MSH` (and any other declared `ignored_standard` segment) never appears
  in `unknown` and never triggers item 3's higher-severity log;
- the mapped fields (`mrn`, `name`, `dob`, `provider`, `location`) are
  byte-for-byte what today's parser already produces — this is
  documentation of the existing loss, not a new mapping;
- the summary is returned in every `/hl7/ingest` response and is not
  persisted or emitted anywhere new, per item 2 above.

## Consequences (if implemented later)

- Turns an invisible, static disclosure into a per-message, structured one —
  a chart pulled from a message that actually carried allergy/medication
  segments would carry a machine-readable marker that evidence was dropped,
  rather than relying on callers to already know the parser's fixed
  limitation.
- Directly supports closing `AUD-04`'s compounding effect on `AUD-09`
  (duplicate patients): a future match-key/reconciliation pass (`adr/0004`)
  could use the per-message "evidence dropped" marker to flag charts that
  may be missing allergy data specifically, rather than only the identity
  mismatch.
- Adds a schema/versioning maintenance cost: every new segment type the
  hospital feed starts sending needs an explicit classification decision
  instead of silently falling through.
- Does not by itself reduce clinical risk — allergies/medications remain
  unparsed until a separate, explicitly authorized implementation follows.

## Non-goals of this ADR

- No parser code change is included in this commit.
- No schema migration is included in this commit.
- Does not implement AL1/RXA field mapping.
- Does not resolve `AUD-09` (duplicate patients) — only removes one
  contributing blind spot for a future pass to use.

## Related

- `docs/planning/hl7-segment-comprehension-week6-08-25-2026.md` — the
  current-state comprehension report this ADR proposes fixing.
- `adr/0004-master-patient-index-match-key.md` — `AUD-04`/`AUD-09`
  compounding context.
- `tests/test_hl7_parser.py` — characterization tests for current behavior
  and the strict `xfail` for the still-undelivered desired behavior.
