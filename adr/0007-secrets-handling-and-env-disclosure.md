# ADR 0007 — Secrets handling, and the disclosure of three committed values

**Date:** 2026-08-20
**Status:** Accepted
**Context:** 2026-08-28 HIPAA-readiness closure, item C2

## Context

`.env` was tracked in git. It carried three values that are not placeholders:

| Variable | Character |
|---|---|
| `DB_PASSWORD` | the application's database password |
| `PAYER_API_KEY` | a `pyr_live_`-prefixed payer clearinghouse key |
| `SESSION_SECRET` | the secret signing browser sessions |

`.gitignore` did not exclude `.env`, so the file was committed and every clone
carries it. This was found during the 2026-08-20 compliance review, not by a
scanner — there is no secret scanning in CI (tracked separately as C6).

Two things were already correct and are unchanged by this decision:
`INTERNAL_SERVICE_TOKEN` is deliberately absent from the committed file and is
validated at startup by six services, and `.env.example` ships empty rather
than with placeholder values.

## Decision

**Untrack and ignore `.env`; rotate the three values; do not rewrite git
history.**

1. `.env` is removed from the index and excluded by `.gitignore`. The local
   file is untouched — it is how a working machine is configured.
2. `docker-compose.yml` no longer defaults `POSTGRES_PASSWORD` to `changeme`.
   It uses the `${VAR:?message}` form, so a missing value stops compose rather
   than booting a stack on a guessable credential.
3. The three values are treated as **disclosed** and must be rotated wherever
   they are used.
4. Git history is **not** rewritten.

## Why not rewrite history

The values remain recoverable from history, and that is a knowing trade, not an
oversight. A `filter-repo` rewrite mid-engagement invalidates every existing
clone and every open branch, needs coordination with anyone holding the
repository, and would land four working days before a demo. The dataset is a
training simulation, so the realistic blast radius of the values themselves is
the repository's access list.

**The consequence must be stated rather than implied: anyone with repository
access, or any old clone, still holds the original values.** Rotation is
therefore the control here — not deletion. Deleting the file without rotating
would change nothing except how easy the values are to find.

If this were a production system holding real PHI, the answer would be
different: rotate first, then rewrite history, then treat it as a reportable
incident.

## Consequences

- A fresh clone no longer has a working `.env` and must copy `.env.example` and
  supply values. Compose now fails fast and says which variable is missing,
  which is the intended behaviour rather than a regression.
- Rotation is a human step, tracked in the pull request. It is not automatable
  here and is not claimed as done by this ADR.
- `tests/test_secrets_hygiene.py` guards the regression: `.env` tracked again,
  the `!.env.example` rule lost, a live-secret shape in a tracked file, or the
  `changeme` default returning.
- Still open, deliberately out of this change: per-service database credentials
  (one shared credential today), and CI secret/dependency/container scanning —
  which would have caught this. Both are C2/C6 items.

## Alternatives considered

**Rewrite history with `git filter-repo`.** Cleanest end state; rejected on
timing and coordination cost, as above. Reconsider after the 2026-09-04
handover.

**Leave `.env` tracked and rotate only.** Rejected: it guarantees the next
value committed is exposed too, and the ignore rule is what makes rotation
durable rather than a one-off.

**Broad entropy-based secret scanning in the test suite.** Rejected as the
primary guard: this repository is full of password *hashes* and base64 test
fixtures, so a high-recall matcher is noisy, and a noisy guard gets deleted
instead of fixed. The suite matches a small set of unambiguous credential
shapes; real scanning belongs in CI (C6) where it can be tuned without
blocking developers.
