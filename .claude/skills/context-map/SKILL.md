---
name: context-map
description: Analyze a repository, platform, or business domain and write dated context-map report and analysis-plan documents without changing code or system configuration. Use when identifying actors, systems, bounded contexts, ownership, trust boundaries, sensitive-data flows, upstream and downstream dependencies, integration relationships, or changes since a previous context map.
---

# Context Map Skill

## Purpose

Show how the subject relates to users, teams, systems, domains, vendors, and data flows, and preserve dated maps so boundary changes can be measured over time.

Treat “context map” as ambiguous until the intended mode is established:

- **System context mode:** map actors, the system of interest, external systems, trust boundaries, and major flows. Use this by default.
- **DDD context mode:** map bounded contexts, ownership, upstream/downstream direction, and relationship patterns. Use this when bounded contexts are explicitly requested or supported by domain evidence.

## Mandatory Output Contract

- Operate in read-only analysis mode except for creating the required documents under `docs/analysis/`.
- Do not modify application code, tests, schemas, configuration, infrastructure, dependencies, logs, source documentation, or existing analysis reports.
- Produce exactly two new Markdown documents for a completed run: the dated context-map report and the dated context-map analysis plan.
- Recommendations may describe future changes, but do not implement them.
- Do not treat a response shown only in chat as completion. The skill is complete only after both files are written to disk and their paths and non-empty contents are verified.
- If file creation is blocked or a same-day file already exists, stop without claiming completion and report the exact blocker and intended paths.

## Process

1. Confirm the system or domain of interest, audience, and decision the map must support.
2. Confirm whether the map represents current state, target state, or both. Keep current and target maps separate.
3. Select system context mode or DDD context mode and state the interpretation.
4. Find the most recent `docs/analysis/context-map-*.md` report, when one exists, and use it only as a comparison point. Re-verify current relationships from source evidence.
5. Inspect documentation, routes, schemas, configuration, deployment definitions, integrations, ownership files, and relevant entry points.
6. Identify relationships and direction before drawing boxes.
7. Label each relationship with purpose, protocol or mechanism when known, data classification, and synchronous or asynchronous behavior when material.
8. Mark trust boundaries, external ownership, sensitive-data movement, and unverified relationships.
9. Validate the map against at least one critical end-to-end workflow.
10. Compare the current map with the previous dated map and identify verified boundary or relationship changes.
11. Write the report and analysis-plan files required by the Mandatory Output Contract.
12. Verify that both files exist under the repository-root `docs/analysis/` directory and are non-empty.

## System Context Mode

Include when relevant:

- Human actors and roles
- System of interest and internal boundary
- External systems, vendors, and regulators
- Authentication and authorization boundaries
- PHI, PII, secrets, payment data, or other sensitive flows
- Primary inbound and outbound integrations
- Operational systems such as identity, monitoring, logging, and support tooling
- Ownership and environment boundaries

## DDD Context Mode

Include when supported by evidence:

- Bounded-context name and responsibility
- Owning team or business capability
- Upstream and downstream direction
- Integration mechanism
- Shared Kernel, Customer/Supplier, Conformist, Anti-Corruption Layer, Open Host Service, or Published Language relationships
- Data ownership and consistency expectations
- Translation, duplication, or coupling risks

Do not assign a DDD relationship label merely because two services communicate.

## Diagram Rules

- Use Mermaid so the map remains editable in Markdown.
- Use clear directional edges and short relationship labels.
- Distinguish internal, external, and third-party boundaries.
- Avoid implementation detail that belongs in a container or component diagram.
- Label inferred or unknown relationships instead of presenting them as facts.
- Add a legend when color, line style, or notation carries meaning.
- Redraw the current map from current evidence; do not reuse the previous diagram without verification.

## Output Format

# Context Map

## Report Metadata
Include performed date and timezone, repository, branch or commit, selected mode, current or target state, and limitations.

## 1. Purpose and Selected Mode
State the scope, audience, current versus target state, and system-context or DDD interpretation.

## 2. Evidence and Confidence
List evidence sources and classify material relationships as observed, claimed, inferred, or unknown.

## 3. Delta Since Previous Context Map
When a prior report exists, identify:

- Added or removed actors, systems, bounded contexts, or vendors
- New, removed, or changed relationships and data flows
- Trust-boundary, ownership, protocol, or data-classification changes
- Resolved, unchanged, regressed, and newly discovered gaps
- Scope changes that prevent direct comparison

## 4. Context Diagram
Provide the current Mermaid diagram and legend.

## 5. Actors, Contexts, and Systems
Describe the responsibility and owner of each major node.

## 6. Relationship Catalog
For each important relationship, describe direction, purpose, mechanism, data exchanged, trust boundary, and failure implications.

## 7. Critical Flow Walkthrough
Use one or more workflows to confirm that the map is coherent.

## 8. Gaps, Risks, and Questions
Record ambiguous ownership, undocumented transfers, disputed boundaries, and missing evidence.

## Analysis Plan Document

Write a separate plan containing:

1. Objective and decision supported
2. Confirmed gaps and unknowns from the report
3. Recommended analysis activities in priority order
4. Evidence or stakeholder input needed for each activity
5. Expected document deliverable and acceptance criteria
6. Dependencies, risks, and suggested owner roles

Keep the plan non-executable: propose investigation and documentation work only, not code or configuration changes.

## Save the Analysis

1. Resolve the repository root with `git rev-parse --show-toplevel`; use the current workspace root when Git is unavailable.
2. Use the local calendar date in `MM-DD-YYYY` format.
3. Write the completed report to `docs/analysis/context-map-MM-DD-YYYY.md`.
4. Write the separate plan to `docs/analysis/context-map-plan-MM-DD-YYYY.md`.
5. Examples: `docs/analysis/context-map-07-01-2026.md` and `docs/analysis/context-map-plan-07-01-2026.md`.
6. Create `docs/analysis` when it does not exist; this is the only directory the skill may create.
7. Preserve every earlier dated report and plan. Do not edit or replace prior documents.
8. Do not overwrite an existing same-day report or plan. Report the collision and ask the user to approve alternate filenames.
9. Include the performed date and timezone, repository path, branch or commit when available, reviewed scope, evidence sources, assumptions, limitations, and comparison source.
10. Exclude plaintext credentials, tokens, PHI, sensitive request bodies, and unnecessary personal information.
11. Before finishing, verify both expected paths exist and each file is non-empty. State the two verified paths in the final response.
12. If the user explicitly requests a different output path or format, follow that instruction for both documents.

## Rules

- Scope before drawing.
- Prefer the smallest map that supports the decision.
- Re-verify current relationships instead of copying a prior map.
- Preserve prior reports as immutable measurement points.
- Keep current-state evidence separate from target-state proposals.
- Do not infer a data flow from repository proximity alone.
- Do not present the map as a security, privacy, or compliance certification.
- Do not start services with live credentials or contact production or external systems without explicit approval.
- The only permitted writes are the two new documents required by this skill.
