# ADR 0009 — Recommendation gate for enabling AI summaries for all patients

**Date:** 2026-08-21
**Status:** Accepted (recommendation; the decision is the client's)
**Context:** Week 8 — security, governance and responsible AI

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

**Status: not met, and not closable by engineering.**

- The payer clearinghouse vendor is **unidentified**, with unknown BAA status —
  open across three reporting cycles. `PAYER_API_URL` points at
  `edi.example.com`, a reserved placeholder, so no live integration exists to
  inspect.
- The AI vendor's agreement is **not evidenced anywhere in the repository.**
  "We already share data with the AI vendor" describes a PHI disclosure; under
  164.502(e) that requires a BAA. Whether one exists is a question only the
  client can answer, and it must be answered before the disclosure widens from
  a subset of patients to all of them.

### Precondition 3 — a human approval gate exists

**Status: met for the patient-facing summary; not met for enablement itself.**

The clinician review gate is real and default-deny: a patient sees released
content only when a clinician has explicitly approved it, and the decision is
recorded with the deciding user and timestamp. That is per-summary approval.

What does not exist is a gate on **enablement** — a recorded decision, by a
named owner, that turns the feature on for a population. There is no owner for
any area of this system (no `CODEOWNERS`, no team named anywhere; see
`docs/handover/responsibility-matrix.md`), so there is currently nobody to
record as having decided.

## What "yes" requires

| # | Condition | Owner | Evidence that closes it |
|---|---|---|---|
| 1 | Scrub wired into every LLM and analytics path | engineering | A test proving no unscrubbed payload reaches a provider call |
| 2 | Residual-risk categories reviewed and accepted, or an expert determination under 164.514(b)(1) | client + counsel | A dated, signed determination |
| 3 | AI vendor BAA in place and its scope recorded | client | The executed agreement |
| 4 | Payer clearinghouse identified and its BAA status recorded | client | Same |
| 5 | A named owner for the enablement decision | client | A name in `CODEOWNERS` |
| 6 | Staged rollout, not all patients at once | joint | A pilot cohort and a rollback path |

## Consequences

- The feature stays available to the clinician-reviewed path it has today. No
  capability is removed.
- Enabling for all patients before conditions 1–4 would widen a PHI disclosure
  of unknown contractual standing, and would rest on a de-identification claim
  the repository does not support.
- This ADR is the artifact to put in front of the board. It says what would have
  to be true, not "no".

## Alternatives considered

**Enable for all patients now.** Rejected: the two unresolved BAA questions are
disclosure questions, not paperwork, and the de-identification premise in the
request is factually wrong today.

**Enable for a pilot cohort now.** Reasonable, and the likely next step — but
still gated on condition 1, because the scrub is not on the path yet. Worth
proposing once it is.

**Say nothing and ship it.** Rejected. The client believes privacy is handled.
Leaving that belief in place while building on top of it is the failure this
week exists to prevent.
