# Onboarding Seam Map — Where an AI Feature Would Attach (and Why It Doesn't Yet)

- **Date:** 2026-07-04
- **Purpose:** One-page onboarding aid showing where a future AI-assisted feature
  (e.g. the client's requested assistant) would plug into the existing system.
  This is a map for new engineers, **not a build spec** — it documents a seam,
  it does not create one. No route, service, or LLM call exists as a result of
  this document.
- **Inputs:** `docs/analysis/context-map-07-01-2026.md`,
  `docs/analysis/baseline-architecture-07-01-2026.md`,
  `docs/analysis/system-audit-07-01-2026.md`, `git log` (the reverted
  `ai-orchestrator` service).

## 1. This has been attempted once already

Commit `2a5039d` ("Add ai-orchestrator AI summary box wired to Bedrock (ADR
0003)") added an AI summary service attached at the gateway fan-out point.
Commit `d0905a1` ("Remove ai-orchestrator AI summary service") fully reverted
it — service, ADR, vendor ToS/transcript/de-identification docs, and Bedrock
credentials — with the stated reason: *"The board's AI ask is a forward
deliverable; the handoff baseline carries no AI code."* This map documents the
same seam that attempt used, so a future attempt starts from an accurate
picture of what the seam currently offers instead of rediscovering it.

## 2. The seam: gateway fan-out

```
Browser -> Next.js frontend (:3070) -> Gateway BFF (:8070) -> [ six domain services ]
                                             ^
                                             |
                                   AI seam would attach here
                                   (NOT IMPLEMENTED)
```

The gateway (`services/gateway/app.py`) is the only point in the system that
already sees cross-service, session-authenticated traffic — every domain
service is called through it. That makes it the natural attachment point for
anything that needs to read across intake/records/scheduling/ROI data, the
same point the removed `ai-orchestrator` attached to.

## 3. What the seam has today

- **Session authentication only.** `require_session` confirms a caller is a
  logged-in staff member. It does not check *which* patient's data that
  caller is authorized to see.
- **No per-patient authorization.** Chart reads are not scoped to the
  requesting user's assigned patients (`AUD-02`, IDOR).
- **No service-to-service authentication.** Every domain service accepts
  unauthenticated calls and has its port published to the host, not just the
  gateway's (`AUD-01`).
- **No disclosure/access accounting.** Neither chart reads nor ROI
  fulfillments produce a real "who accessed what, when" trail (`AUD-03`,
  `AUD-07`).

## 4. What the seam would need before any AI feature attaches

- **Patient-scoped authorization** at or before the gateway fan-out point, so
  an AI feature can only act on data the calling user is actually authorized
  to see (closes `AUD-02`).
- **A PHI-safe outbound logging path** for anything the seam calls out to —
  see Commit 3 of this deliverable.
- **A provider-swappable LLM client** with timeout, retry, structured-output
  parsing, and a token/cost guard, so a future feature isn't built directly
  against one vendor's SDK with no failure handling — see Commit 2 of this
  deliverable.
- **A resolved patient-identity story.** Without a master-patient-index
  (`AUD-09`), a chart-summarization feature could summarize only one of
  several fragmented records for the same person with no indication anything
  is missing.
- **A disclosure/authorization framework**, since any AI-mediated read of PHI
  is itself a new access path and current ROI/records access has no
  authorization enforcement to attach it to (`AUD-03`).

## 5. Non-goals of this document

This map does not implement, wire, or scaffold an AI feature. It does not add
a route, a service, or a dependency. It exists so the next engineer (or the
next AI-feature attempt) starts from a documented seam instead of
re-deriving it from source, and so the decision to defer the feature (see
`docs/planning/ai-readiness-debt-log-07-04-2026.md`) is legible without
re-reading three separate analysis reports.
