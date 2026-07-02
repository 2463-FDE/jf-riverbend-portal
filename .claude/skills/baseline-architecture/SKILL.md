---
name: baseline-architecture
description: Analyze a repository or deployed system and write dated current-state architecture report and analysis-plan documents without changing code or system configuration. Use when documenting system boundaries, actors, components, services, data stores, integrations, deployment topology, trust boundaries, critical workflows, operational constraints, architectural debt, or progress since a previous architecture baseline.
---

# Baseline Architecture Skill

## Purpose

Describe the system as it exists today so future designs and changes have a reliable, comparable starting point.

Keep the baseline descriptive rather than turning it into a full risk audit. Record material risks and debt, but use the `system-audit` skill when the primary objective is severity or remediation.

## Mandatory Output Contract

- Operate in read-only analysis mode except for creating the required documents under `docs/analysis/`.
- Do not modify application code, tests, schemas, configuration, infrastructure, dependencies, logs, source documentation, or existing analysis reports.
- Produce exactly two new Markdown documents for a completed run: the dated baseline-architecture report and the dated baseline-architecture analysis plan.
- Recommendations may describe future changes, but do not implement them.
- Do not treat a response shown only in chat as completion. The skill is complete only after both files are written to disk and their paths and non-empty contents are verified.
- If file creation is blocked or a same-day file already exists, stop without claiming completion and report the exact blocker and intended paths.

## Process

1. Confirm the repository, environment, audience, and current-state scope.
2. Ask whether the user also needs a target-state view. Keep it separate from the baseline.
3. Find the most recent `docs/analysis/baseline-architecture-*.md` report, when one exists, and use it only as a comparison point. Re-verify current facts from source evidence.
4. Inspect architecture documentation, entry points, service configuration, routes, schemas, deployment files, tests, runbooks, and relevant history.
5. Identify actors, system boundaries, components, data stores, external systems, ownership, and trust boundaries.
6. Trace critical end-to-end workflows rather than listing components without relationships.
7. Record deployment topology, scaling model, availability dependencies, security boundaries, and operational constraints.
8. Classify statements as:
   - **Observed:** verified directly from code, configuration, or runtime evidence.
   - **Claimed:** stated in documentation or by a stakeholder but not independently verified.
   - **Inferred:** reasonably concluded from evidence and clearly labeled.
   - **Unknown:** important but not established from the available material.
9. Reconcile contradictions instead of silently choosing one source.
10. Compare the current baseline with the previous report and identify verified changes.
11. Produce the architecture diagram and written baseline.
12. Write the report and analysis-plan files required by the Mandatory Output Contract.
13. Verify that both files exist under the repository-root `docs/analysis/` directory and are non-empty.

## Required Coverage

Include when relevant:

- Users, roles, and external actors
- Frontends, gateways, services, jobs, and shared libraries
- Databases, caches, queues, file stores, logs, and backups
- External vendors and integration protocols
- Authentication, authorization, and trust boundaries
- Sensitive-data and credential flows
- Critical request, event, and batch workflows
- Deployment units, regions, networks, and runtime dependencies
- Scaling, availability, recovery, and observability model
- Known constraints, architectural debt, and unresolved decisions

## Diagram Rules

- Prefer a Mermaid flowchart or C4-style system/container view that remains editable in Markdown.
- Show direction on calls and data flows.
- Label synchronous versus asynchronous paths when the distinction matters.
- Mark external systems and trust boundaries explicitly.
- Do not imply verified deployment details when only repository evidence is available.
- Keep current-state and proposed target-state diagrams separate.

## Output Format

# Baseline Architecture

## Report Metadata
Include performed date and timezone, repository, branch or commit, scope, evidence type, and limitations.

## 1. Scope and Evidence
Describe the reviewed scope, evidence sources, exclusions, commit or version, and environment limitations.

## 2. Executive Overview
Summarize the system purpose, principal boundaries, and most important architectural characteristics.

## 3. Delta Since Previous Baseline
When a prior report exists, identify:

- Added, removed, or materially changed components and relationships
- Resolved, reduced, unchanged, regressed, and newly observed constraints
- Evidence or confidence changes
- Items that cannot be compared because scope changed

Do not claim progress from documentation changes alone when implementation or runtime evidence is required.

## 4. System Diagram
Provide the current-state diagram and a short explanation.

## 5. Component Inventory
For each major component, record responsibility, interfaces, data ownership, dependencies, deployment unit, and evidence status.

## 6. Critical Workflows
Describe important end-to-end request, event, integration, and failure paths.

## 7. Data and Trust Boundaries
Document sensitive data, credentials, authentication, authorization, external transfers, storage, logging, and backup boundaries.

## 8. Deployment and Operations
Document environments, topology, scaling, availability dependencies, recovery, monitoring, and ownership.

## 9. Constraints, Debt, and Unknowns
Separate confirmed constraints from inferred or unresolved items.

## 10. Questions and Recommended Next Analysis
List decisions or evidence needed to improve confidence.

## Analysis Plan Document

Write a separate plan containing:

1. Objective and architecture decisions supported
2. Confirmed gaps, constraints, and unknowns from the report
3. Recommended analysis and documentation activities in priority order
4. Evidence or stakeholder input needed for each activity
5. Expected document deliverable and acceptance criteria
6. Dependencies, risks, and suggested owner roles

Keep the plan non-executable: propose investigation and documentation work only, not code or configuration changes.

## Save the Analysis

1. Resolve the repository root with `git rev-parse --show-toplevel`; use the current workspace root when Git is unavailable.
2. Use the local calendar date in `MM-DD-YYYY` format.
3. Write the completed report to `docs/analysis/baseline-architecture-MM-DD-YYYY.md`.
4. Write the separate plan to `docs/analysis/baseline-architecture-plan-MM-DD-YYYY.md`.
5. Examples: `docs/analysis/baseline-architecture-07-01-2026.md` and `docs/analysis/baseline-architecture-plan-07-01-2026.md`.
6. Create `docs/analysis` when it does not exist; this is the only directory the skill may create.
7. Preserve every earlier dated report and plan. Do not edit or replace prior documents.
8. Do not overwrite an existing same-day report or plan. Report the collision and ask the user to approve alternate filenames.
9. Include the performed date and timezone, repository path, branch or commit when available, reviewed scope, evidence sources, assumptions, limitations, and comparison source.
10. Exclude plaintext credentials, tokens, PHI, sensitive request bodies, and unnecessary personal information.
11. Before finishing, verify both expected paths exist and each file is non-empty. State the two verified paths in the final response.
12. If the user explicitly requests a different output path or format, follow that instruction for both documents.

## Rules

- Prefer repository and runtime evidence over stale documentation.
- Re-verify the current system; do not merely copy the previous report.
- Preserve prior reports as immutable measurement points.
- Do not present a baseline as a security, privacy, or compliance certification.
- Do not invent ownership, topology, protocols, or service-level objectives.
- Keep observations, claims, inferences, and unknowns distinguishable.
- Do not start services with live credentials or contact production or external systems without explicit approval.
- The only permitted writes are the two new documents required by this skill.
