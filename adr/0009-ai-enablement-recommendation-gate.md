# ADR 0009 — Recommendation gate for enabling AI summaries for all patients

**Date:** 2026-08-21
**Status:** Accepted (recommendation; the decision is the client's)
**Context:** Week 8 — security, governance and responsible AI

> **Scope, established 2026-08-21.** This is a **synthetic training project**.
> Local Docker Compose only, no production hosting target, no real patient data,
> and no live payer or clinical integration. Bedrock is used solely as an
> external model-inference provider for synthetic-data testing.
>
> This ADR therefore reasons about **the pattern**, not a live disclosure. It
> does not assert that any BAA is executed, nor that one is required for an
> integration that does not exist. Where it names a vendor condition, that
> condition applies **if and only if** this system were ever used with real
> patient data.

## The request

The board wants the AI visit-summary enabled automatically for every patient as
the headline feature. The stated reasoning: *"We already share data with the AI
vendor, and we have a de-identified export for analytics, so the privacy stuff
is handled."*

## The recommendation

**Do not enable it for all patients yet.** Three preconditions are unmet, and
each is checkable rather than a matter of judgement.

This is not a refusal of the feature. It is a gate with named conditions; when
they are met, the answer changes.

### Precondition 1 — patient data is scrubbed before it reaches a model

**Status: partially met, as of this ADR.**

`libs/deid` now removes mechanically-detectable direct identifiers from text and
structured payloads, with 26 tests. What it does **not** do is make the data
de-identified under 45 CFR 164.514(b)(2), and no artifact built on it may say
so. Two categories cannot be closed by pattern matching:

- **(A) names** — a third party mentioned in narrative ("her daughter Ana drove
  her") is not detected; only the subject's own name parts, supplied from the
  record, are removed;
- **(R) any other unique identifying number, characteristic or code** — open
  ended by definition. A rare diagnosis at a small clinic identifies someone,
  and no pattern finds that.

Also unmet: **the scrub is not yet wired into the LLM or analytics paths.** It
exists and is tested; nothing calls it. Until it is on the path, the control is
theoretical.

**The claim that "we have a de-identified export for analytics" is not
supported by this repository.** No de-identification code existed before this
change, so any existing export was of identified data. That is the finding, and
it should be corrected with the client before it is repeated to a board.

### Precondition 2 — vendor rules are clear

**Status: not applicable in this simulation; would be mandatory in real use.**

- **There is no payer clearinghouse, live endpoint or real payer data.**
  `PAYER_API_URL` points at `edi.example.com`, a reserved placeholder.
  `PAYER_API_KEY` is deliberately blank and stays blank. Eligibility behaviour
  is **simulated** and should be labelled that way on screen. No BAA is executed
  and none is represented as required, because there is no integration.
  *(One engineering item survives: the placeholder is still wired to a live
  `httpx` call, so a check attempts a real outbound request and fails. That
  needs a simulation mode — it is work, not a governance question.)*
- **Model inference uses Bedrock on synthetic data only.** No real PHI leaves
  the boundary, so there is no disclosure to paper. What remains worth stating
  is the pattern: **in real use, sending patient narrative to a third-party
  model is a PHI disclosure requiring a BAA under 164.502(e).** That sentence is
  the deliverable here — a design note for whoever would operate this, not a
  claim about an agreement.
- **The client's premise still needs correcting**, and this is the part that is
  true regardless of scope: "we have a de-identified export for analytics" was
  not supported by the repository, because no de-identification code existed
  before this change.

### Precondition 3 — a human approval gate exists

**Status: met for the patient-facing summary; not met for enablement itself.**

The clinician review gate is real and default-deny: a patient sees released
content only when a clinician has explicitly approved it, and the decision is
recorded with the deciding user and timestamp. That is per-summary approval.

What does not exist is a gate on **enablement** — a recorded decision that
turns the feature on for a population. In this training environment there is no
production operational ownership to attribute such a decision to, and none is in
scope (see `docs/handover/responsibility-matrix.md`). The design point stands
for real use: enablement should be an attributable decision, not a config flag
somebody flips.

## What "yes" requires

| # | Condition | Owner | Evidence that closes it |
|---|---|---|---|
| 1 | Scrub wired into every LLM and analytics path | engineering | A test proving no unscrubbed payload reaches a provider call |
| 2 | Residual-risk categories reviewed and accepted, or an expert determination under 164.514(b)(1) | client + counsel | A dated, signed determination — **only if real data is ever used** |
| 3 | AI vendor BAA in place and its scope recorded | client | **N/A in simulation.** Prerequisite if real patient data is ever sent to a model |
| 4 | Payer clearinghouse identified and its BAA status recorded | client | **N/A in simulation** — no clearinghouse exists |
| 5 | A named owner for the enablement decision | client | **N/A in simulation** — no production operational ownership is in scope |
| 6 | Staged rollout, not all patients at once | joint | A pilot cohort and a rollback path |

## Consequences

- The feature stays available to the clinician-reviewed path it has today. No
  capability is removed.
- In this simulation, the substantive blocker is **condition 1 alone**: the
  scrub is not on the LLM path, so the control is theoretical. Conditions 3–5 do
  not apply here and are recorded as prerequisites for real use.
- The de-identification claim in the original request remains unsupported by the
  repository, and that is worth saying regardless of scope.
- This ADR is the artifact to put in front of the board. It says what would have
  to be true, not "no".

## Alternatives considered

**Enable for all patients now.** Rejected on condition 1: the scrub exists but
nothing calls it, so "patient data is scrubbed before it reaches a model" is not
true yet. The de-identification premise in the request is also factually wrong
today, which is worth correcting whether or not the data is synthetic.

**Enable for a pilot cohort now.** Reasonable, and the likely next step — but
still gated on condition 1, because the scrub is not on the path yet. Worth
proposing once it is.

**Say nothing and ship it.** Rejected. The client believes privacy is handled.
Leaving that belief in place while building on top of it is the failure this
week exists to prevent.
