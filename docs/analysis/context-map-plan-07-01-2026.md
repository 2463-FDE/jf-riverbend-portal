# Context Map Analysis Plan

Companion plan to `docs/analysis/context-map-07-01-2026.md`. This document proposes further investigation and documentation work only — it does not specify or authorize any code or configuration change.

## 1. Objective and Decision Supported

Give the team taking over this handoff a validated picture of actors, system boundary, external systems, and trust boundaries so that any remediation, compliance review, or audit prioritization work starts from confirmed facts rather than the README's self-asserted compliance claim. This plan lists the follow-up analysis needed to close the gaps the context map could not resolve from static inspection alone.

## 2. Confirmed Gaps and Unknowns From the Report

1. Whether patients have (or are intended to have) an independent registration/login path, versus staff-assisted-only intake (Section 8).
2. Whether `HL7_FEED_HOST`/`HL7_FEED_PORT` reflect a planned real-time hospital feed integration or stale/abandoned config.
3. The compliance-claim contradiction between `README.md` and `adr/0002`/`ARCHITECTURE.md` §7 — no internal document resolves which is authoritative.
4. No internal ownership (team/individual) is established for any service, table, or compliance area.
5. Whether the "gateway is the only entry point" boundary is enforced anywhere beyond convention (frontend always calling gateway) given every domain service's port is also published to the host.
6. Real identity of the payer clearinghouse vendor and the actual contractual/BAA terms in place (only a placeholder domain and generic env vars were found in code).

## 3. Recommended Analysis Activities (priority order)

1. **Confirm the patient-registration model** (High priority — resolves the largest actor-model ambiguity). Walk the live `/intake` flow in a running `docker compose up` environment as an unauthenticated browser session and observe whether it's reachable without a staff login. This directly resolves Gap 1.
2. **Interview Riverbend stakeholders (e.g., the COO/ops contact referenced in handover material) on HL7 feed intent** (Medium priority). A short conversation resolves Gap 2 — whether to plan for a real-time listener or simply remove the dead config in a future change.
3. **Reconcile the compliance-claim contradiction into a single source of truth** (High priority — compliance/legal exposure). Have whoever owns compliance for Riverbend formally acknowledge which document (README vs. ADR/ARCHITECTURE.md) is authoritative, and correct the losing one. This is a documentation-accuracy activity, not a system change.
4. **Establish internal ownership** (High priority — blocks any prioritization conversation). Identify or assign internal owners per service/table area described in `ARCHITECTURE.md` §2, and record it in a `CODEOWNERS` file or equivalent. (Creating that file is a follow-up documentation activity, not something this plan performs.)
5. **Verify network-boundary enforcement** (Medium priority). Confirm in a running environment whether anything other than the gateway can currently reach a domain service's published port from outside the compose network, to settle whether Gap 5 is a real exposure or a non-issue in the actual deployment environment.
6. **Confirm the payer clearinghouse vendor and contract/BAA status** (Medium priority — compliance-relevant). Locate or request the actual vendor agreement; `docs/handover/payer-status-page.md` names "ACME Clearinghouse" against a placeholder domain, which may itself be a handover artifact rather than the real vendor.

## 4. Evidence or Stakeholder Input Needed per Activity

| Activity | Evidence / input needed |
|---|---|
| 1. Confirm patient-registration model | A running instance (`make up`) and a browser test of `/intake` unauthenticated; alternatively, direct confirmation from Riverbend product/ops staff |
| 2. HL7 feed intent | Conversation with Riverbend IT/ops or the hospital-system contact; no code-only answer exists |
| 3. Reconcile compliance claim | Sign-off from whoever at Riverbend owns compliance/legal responsibility |
| 4. Establish ownership | Riverbend engineering management decision on who owns what post-handoff |
| 5. Network-boundary verification | Running environment + inspection of actual deployment network config (not just `docker-compose.yml`, since "production" per `ARCHITECTURE.md` is a per-clinic VM whose real network topology wasn't found in this repo) |
| 6. Payer vendor/contract confirmation | Riverbend's actual vendor contract, or confirmation from whoever manages the payer relationship |

## 5. Expected Document Deliverable and Acceptance Criteria

- Activities 1, 2, and 5 should produce a short addendum or a revised dated context map (`context-map-MM-DD-YYYY.md`) once the relevant unknown is resolved with observed evidence — acceptance criterion: the corresponding row in Section 2 of the map moves from "unresolved/unknown" to "observed," with a citation.
- Activity 3 should produce a single corrected authoritative compliance statement (in whichever of `README.md` or the ADR set is designated authoritative) — acceptance criterion: the two documents no longer disagree.
- Activity 4 should produce a `CODEOWNERS` file or equivalent ownership record — acceptance criterion: every service/table area in `ARCHITECTURE.md` §2 has a named internal owner.
- Activity 6 should produce a short note (internal, not necessarily in this repo) confirming vendor identity and BAA/contract status — acceptance criterion: the payer relationship is no longer described only via a placeholder domain in code.

## 6. Dependencies, Risks, and Suggested Owner Roles

- **Dependencies:** Activities 3 and 4 likely need Riverbend engineering leadership involvement, not just the engineer(s) executing this handoff; they are organizational decisions this analysis cannot make unilaterally.
- **Risks:** Activity 1 (live-testing the intake flow) should be run against a local `docker compose up` environment with seed/demo data only — not against any environment holding real patient data — to avoid creating or exposing real PHI during verification.
- **Suggested owner roles:** Activities 1, 2, 5 — engineering (whoever picks up this handoff). Activity 3 — compliance/legal or whoever at Riverbend is accountable for HIPAA posture. Activity 4 — engineering management. Activity 6 — whoever manages vendor/payer relationships at Riverbend (likely outside the engineering team).
