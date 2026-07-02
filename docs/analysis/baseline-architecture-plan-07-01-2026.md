# Baseline Architecture Analysis Plan

Companion plan to `docs/analysis/baseline-architecture-07-01-2026.md`. This document proposes further investigation and documentation work only — it does not specify or authorize any code or configuration change.

## 1. Objective and Architecture Decisions Supported

Close the evidence gaps that keep this baseline descriptive-but-incomplete in a few places (Section 9's Unknown/Inferred items), so future architecture decisions — e.g., where to introduce a queue for the eligibility call, how to enforce the gateway-only trust boundary, whether to build a real patient-registration path — start from confirmed facts about the current system rather than repository-only inference.

## 2. Confirmed Gaps, Constraints, and Unknowns From the Report

1. Patient-registration/actor model is unresolved (shared with the context-map and system-audit reports — this is the third report to flag it).
2. `docs/runbook.md` was not read in full for this baseline; its operational/recovery content is unverified against Section 8.
3. HL7-derived data's downstream persistence path (past `interop-service`'s parse step) was not traced.
4. The real clinic-VM production network topology is unknown (shared with the system-audit's AUD-01 open question).
5. Backup/retention policy for Postgres, Redis, and `logs/` is undocumented in-repo.
6. `HL7_FEED_HOST`/`HL7_FEED_PORT` intent is unresolved (shared across all three reports produced today — resolve once).
7. Payer clearinghouse vendor identity/contract status is unresolved (shared with the context-map plan).

## 3. Recommended Analysis Activities (priority order)

1. **Trace HL7-derived data persistence** (High priority, repository-only — no stakeholder needed). Search beyond `services/interop-service` for any consumer of its parsed output, or confirm it is genuinely a stateless parse-and-return endpoint with persistence happening elsewhere (e.g., does the gateway or another service write the parsed record to Postgres after `interop-service` returns it?). This is answerable from the repository alone and should be resolved before treating Section 6's HL7 workflow description as complete.
2. **Read `docs/runbook.md` in full** (Medium priority, repository-only). Reconcile its recovery/operational claims against Section 8; update a future baseline revision if it reveals topology or process detail not captured here.
3. **Confirm the patient-registration/actor model** (High priority, needs stakeholder input — same activity as already recommended in the context-map plan; do not commission separately).
4. **Confirm real deployment network topology** (Medium priority, needs stakeholder/environment access — same activity as the system-audit plan's Activity 2; shared, not duplicated).
5. **Resolve HL7 feed config intent and payer vendor/contract status** (Low priority, needs stakeholder input — same activities already listed in the context-map plan; shared, not duplicated).
6. **Document backup/retention policy** (Low priority, needs stakeholder/ops input — likely requires someone with access to the actual hosting environment, since nothing in-repo answers this).

## 4. Evidence or Stakeholder Input Needed per Activity

| Activity | Evidence / input needed |
|---|---|
| 1. Trace HL7 persistence | Repository-only: search gateway and other services for any call consuming `interop-service`'s response, or confirm none exists |
| 2. Read runbook | Repository-only: `docs/runbook.md` full read |
| 3. Confirm patient actor model | Riverbend product/ops stakeholder, or a live walkthrough of `/intake` unauthenticated (shared with context-map plan Activity 1) |
| 4. Confirm network topology | Whoever manages the real clinic-VM deployment (shared with system-audit plan Activity 2) |
| 5. HL7/payer vendor resolution | Riverbend IT/ops and vendor-relationship stakeholders (shared with context-map plan Activities 2 and 6) |
| 6. Backup/retention policy | Whoever manages the hosting/ops environment for Postgres/Redis in practice |

## 5. Expected Document Deliverable and Acceptance Criteria

- Activity 1 should update Section 6 (Critical Workflows) and Section 9 (Inferred) of a future dated baseline revision once the HL7 persistence path is confirmed one way or the other — acceptance criterion: the workflow description no longer says "not traced further in this pass."
- Activity 2 should either confirm Section 8 is consistent with the runbook or produce a list of discrepancies to reconcile in the next baseline revision.
- Activities 3-5 should each move the corresponding Section 9 item from Unknown to Observed (with citation) in a future baseline, context-map, or system-audit revision — whichever is produced next; there's no need to duplicate the resolution across all three document families once it's confirmed.
- Activity 6 should produce a short internal note on backup/retention policy (wherever Riverbend keeps such operational documentation — not necessarily in this repository).

## 6. Dependencies, Risks, and Suggested Owner Roles

- **Dependencies:** None of these activities block each other; they can proceed independently and in parallel.
- **Risks:** None specific to this plan beyond the general caution (shared with the other two reports' plans) that any live-environment verification (Activities 3, 4) should not be performed against real patient data or real production credentials without explicit approval.
- **Suggested owner roles:** Activities 1, 2 — engineering (repository-only, no external dependency). Activity 3 — engineering + Riverbend product/ops. Activity 4 — whoever manages the clinic-VM deployment (ownership not established in this repository — see the context-map's ownership gap). Activities 5, 6 — Riverbend IT/ops and vendor-relationship stakeholders, not engineering alone.
