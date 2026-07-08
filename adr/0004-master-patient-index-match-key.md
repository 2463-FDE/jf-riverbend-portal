# ADR 0004 — Master-patient-index / match-key approach for AUD-09 (proposal)

- **Status:** Proposed — not implemented. This ADR is a proposal only; no
  schema change, no matching logic, and no production retrieval helper are
  included in this ADR or its accompanying commit.
- **Date:** 2026-07-08
- **Author:** Week 2 AI-readiness deliverable (this proposal). Unlike ADRs
  0001-0003, this is not authored by Helix Digital Partners (the original
  contractor) — no internal Riverbend team name exists in this repo to
  attribute it to either (see `CLAUDE.md`, "Unknowns").

## Context

- `AUD-09` (`docs/analysis/system-audit-07-01-2026.md`): no unique match key
  on (name, dob, ssn); `intake.yaml: match_key: none`; every `/intake` call
  creates a brand-new `patients` row regardless of whether the same person
  already has one.
- Real-world manifestation: `RIV-160` — Maria Gonzalez exists as three
  separate patient rows (1042, 1330, 1588; see
  `db/seed/generate_seed.py`), with a penicillin allergy recorded only under
  1330. A clinician opening 1042's chart sees "no known allergies" with no
  indication that another chart row for the same person says otherwise.
  `AUD-04` (the HL7 parser silently dropping allergy/medication segments)
  compounds this same fixture in the real incident.
- `docs/planning/gold-set-risk-log-07-08-2026.md` (`RISK-GS-01`) explains why
  recall@k/precision@k alone can look acceptable while still missing a
  patient's other fragments.
- `docs/planning/retrieval-eval-report-07-08-2026.md` demonstrates this
  concretely: at `top_k=1`, the eval harness's fragment-coverage-gap metric
  is 66.7%, driven entirely by the Maria Gonzalez fragmentation — both gold-
  set questions about her hit this gap, while the non-fragmented James
  O'Brien case does not.
- The client's actual ask (a retrieval helper that surfaces relevant past
  records at chart-open) cannot be built safely on top of this: a
  summarization/retrieval feature would inherit whichever single chart row
  it happens to be pointed at, with no signal that it might be incomplete.

## Decision (proposed)

Propose the following match-key strategy for a future, separately-scoped
implementation. Nothing below is implemented by this ADR or this commit.

1. **Deterministic match key at intake.** Normalize `(last_name, first_name,
   dob, ssn_last4)` into a candidate key; on `/intake`, query existing
   patients for a match before creating a new row (the hook `intake.yaml:
   match_key` already exists and is currently set to `none` — this proposal
   is what would populate it).
2. **Tiered confidence, not a binary match/no-match:**
   - **Exact match** (dob + full ssn agree): flag as a certain duplicate;
     block silent creation of a new row and require staff confirmation
     before proceeding either way.
   - **Partial match** (e.g., ssn agrees but name/dob differ slightly — the
     exact Maria Gonzalez fixture pattern: same SSN, three name spellings,
     one differing DOB): surface as a "possible duplicate" for staff review.
     Do not auto-merge on a partial match.
   - **No match:** proceed as a new patient, exactly as today.
3. **Non-destructive linking.** Record confirmed identity links in a new
   `patient_links` table (or a `patients.mpi_id` column) rather than
   rewriting `patient_id` foreign keys across `encounters`/`records`/
   `appointments`/`roi_requests`/etc. This preserves who linked what, when,
   and on what basis, instead of silently collapsing history.
4. **No retroactive merge of existing duplicates.** Adopting this match key
   going forward does not, by itself, merge the Maria Gonzalez fixture's
   three existing rows (or any other already-fragmented patient in
   production). Backfilling/reconciling historical duplicates is a separate,
   larger, and higher-risk effort — it touches every table with a
   `patient_id` foreign key — and is explicitly out of scope for this ADR.

## Consequences (if implemented later)

- Closes the specific compounding effect described in `AUD-04`/`AUD-09`/
  `RIV-160`: an HL7-sourced chart silently missing allergy data becomes
  distinguishable from "patient has no known allergies," because staff are
  prompted at intake instead of the system creating a fourth fragment.
- Directly unblocks a future retrieval/summarization feature from
  confidently citing only one of several fragments — the precondition
  `docs/planning/onboarding-seam-map.md` §4 already named as missing.
- Adds intake-path latency/complexity (a lookup, and sometimes a
  staff-confirmation step). This should be evaluated together with `AUD-11`
  (the existing synchronous, no-timeout payer-eligibility call already
  blocking `/intake`) so the two latency problems aren't compounded without
  a plan — likely argues for doing the match-key lookup and the eligibility
  call asynchronously/in parallel rather than serially on the same request.
- Requires a separate backfill/reconciliation decision for already-
  fragmented historical patients before this closes `AUD-09` completely for
  existing data — not scoped here.
- Does not, by itself, fix `AUD-02` (IDOR) or `AUD-03` (ROI authorization) —
  those remain independent findings with their own recommended fixes in
  `docs/analysis/system-audit-07-01-2026.md`.
- A false-positive partial match (staff incorrectly confirms two different
  people as the same patient) is a new risk this proposal introduces; the
  non-destructive-linking design (item 3) is intended to make such an error
  reviewable/reversible rather than a silent, permanent data merge, but the
  review workflow itself is not designed in this ADR.

## Non-goals of this ADR

- No schema migration is included in this commit.
- No matching logic is implemented in this commit.
- No production retrieval helper is implemented in this commit.
- No existing patient row is merged or altered — the Maria Gonzalez fixture
  remains three rows after this ADR.
- Does not resolve `AUD-02`, `AUD-03`, or any other system-audit finding
  besides `AUD-09`.

## Related

- `docs/analysis/system-audit-07-01-2026.md` — `AUD-09`, `AUD-04`, `AUD-11`.
- `docs/planning/gold-set-risk-log-07-08-2026.md` — `RISK-GS-01`.
- `docs/planning/retrieval-eval-report-07-08-2026.md` — the eval run this
  proposal is grounded in.
- `docs/planning/onboarding-seam-map.md` and
  `docs/planning/retrieval-eval-seam-map-07-08-2026.md` — seam context.
- `libs/rag_eval/identity_proxy.py` — the eval-only heuristic proxy this ADR
  is explicitly not proposing as the production approach.
