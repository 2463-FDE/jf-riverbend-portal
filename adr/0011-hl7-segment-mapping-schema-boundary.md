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
   `SEGMENT_MAP` dict with an explicit, versioned schema (e.g. one entry per
   segment type stating: field indices, whether the segment is
   `mapped` / `recognized_unmapped` / `unknown`, and — for
   `recognized_unmapped` — a named reason). `AL1` and `RXA` would move from
   silently absent to explicitly `recognized_unmapped`, distinct from a
   truly unknown segment type.
2. **Per-message comprehension result, not a static note.** Replace
   `app.py`'s hardcoded `UNMAPPED_NOTE` string with a per-message summary
   computed from what the message actually contained: which segments were
   mapped, which were recognized-but-unmapped (and why), and which were
   unrecognized. This makes the current data loss visible per-message
   instead of only in a fixed disclosure string that never changes.
3. **Fail loudly on a genuinely unrecognized segment, not just skip it.**
   Distinguish "known segment type, not mapped by policy" (skip silently,
   as today, but recorded) from "segment type never seen before" (log at a
   higher severity so a new hospital-feed segment type doesn't disappear
   unnoticed the way `AL1`/`RXA` did).
4. **Implementing AL1/RXA mapping itself is a separate, later decision.**
   This ADR proposes making the drop *visible and structured*; it does not
   propose or schedule actually parsing allergy/medication content. That
   remains explicitly deferred pending separate authorization, per
   `CLAUDE.md`'s standing instruction not to opportunistically fix
   documented debt.

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
