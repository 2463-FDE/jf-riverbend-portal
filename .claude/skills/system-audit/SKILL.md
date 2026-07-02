---
name: system-audit
description: Analyze a repository, service, feature, or pull request and write dated system-audit report and analysis-plan documents without changing code or system configuration. Use when evidence-based findings, severity, containment, remediation order, acceptance criteria, production readiness, or longitudinal risk comparison are needed.
---

# System Audit Skill

## Purpose

Inspect the system for evidence-based risks and gaps, then preserve a dated report so remediation progress and regressions can be measured over time.

Identify when relevant:

- Security and authorization issues
- Correctness and data-integrity bugs
- Missing or ineffective tests
- Reliability and recovery risks
- Configuration and secret-handling problems
- Production-readiness and documentation gaps

## Mandatory Output Contract

- Operate in read-only analysis mode except for creating the required documents under `docs/analysis/`.
- Do not modify application code, tests, schemas, configuration, infrastructure, dependencies, logs, source documentation, or existing analysis reports.
- Produce exactly two new Markdown documents for a completed run: the dated system-audit report and the dated system-audit analysis plan.
- Recommendations may describe future remediation, but do not implement it.
- Do not treat a response shown only in chat as completion. The skill is complete only after both files are written to disk and their paths and non-empty contents are verified.
- If file creation is blocked or a same-day file already exists, stop without claiming completion and report the exact blocker and intended paths.

Treat a pull-request audit as a risk-focused assessment of changed behavior and surrounding controls. Do not replace a line-by-line code review when the user requests review comments.

## Process

1. Confirm the repository, environment, scope, audience, and decision the audit must support.
2. Record exclusions and whether the audit is repository-only or includes runtime evidence.
3. Find the most recent `docs/analysis/system-audit-*.md` report, when one exists, and use it only as a comparison point. Re-verify every current finding from source evidence.
4. Inspect code, configuration, routes, schemas, migrations, tests, deployment files, runbooks, documentation, and relevant history.
5. Trace critical success and failure paths across service boundaries.
6. Check whether security and correctness are enforced on the backend rather than assumed by the frontend.
7. Check tests for important success, authorization, failure, retry, timeout, race, and recovery paths.
8. Check configuration, environment variables, secret handling, network exposure, logs, backups, and external integrations.
9. Classify evidence and assign severity using the rubrics below.
10. Separate immediate containment from permanent remediation.
11. Compare current findings with the prior audit using stable finding IDs.
12. Recommend concrete fixes, acceptance criteria, owners when known, and validation commands without executing changes.
13. Write the report and analysis-plan files required by the Mandatory Output Contract.
14. Verify that both files exist under the repository-root `docs/analysis/` directory and are non-empty.

## Evidence Classification

- **Confirmed:** directly demonstrated by code, configuration, tests, logs, or runtime evidence.
- **Inferred:** strongly suggested by evidence but dependent on an unverified condition.
- **Claimed:** stated by documentation or a stakeholder but not independently verified.
- **Unknown:** material to the conclusion but unavailable within the audit scope.

Never convert an inferred or claimed issue into a confirmed finding without evidence.

## Severity Rubric

- **Critical:** credible immediate compromise, cross-tenant or broad sensitive-data exposure, destructive integrity loss, or platform-wide outage with no effective control.
- **High:** substantial confidentiality, integrity, availability, safety, or authorization failure that is exploitable or likely under realistic conditions.
- **Medium:** meaningful weakness with limited impact, additional preconditions, or effective compensating controls.
- **Low:** defense-in-depth, maintainability, documentation, or minor operational issue with limited direct impact.

Adjust severity for exposure, data sensitivity, privilege required, blast radius, detectability, recoverability, and compensating controls. Explain material uncertainty.

## Audit Areas

Review when relevant:

- Authentication, authorization, IDOR, and tenant isolation
- Input validation and frontend-only security assumptions
- Secrets, environment variables, and sensitive logging
- Error handling, retry logic, timeouts, and rate limiting
- Database constraints, migrations, races, and idempotency
- API gateway and service-boundary behavior
- Test coverage and CI/CD gates
- Docker, infrastructure, backup, and recovery exposure
- Documentation accuracy and production claims

## Safety and Data Handling

- Use read-only inspection by default.
- Do not print, copy, or place plaintext credentials, tokens, PHI, sensitive request bodies, or unnecessary personal information in the report.
- Refer to sensitive evidence by file, field, category, or redacted fingerprint rather than reproducing values.
- Do not start services with handed-over or live credentials without explicit approval.
- Do not call production, payer, model, cloud, or other external systems without explicit approval.
- Do not delete or rewrite possible incident evidence. Separate containment from evidence-preservation decisions.
- Do not describe an audit as a penetration test, legal opinion, HIPAA certification, or compliance certification unless explicitly authorized and performed by qualified parties.

## Output Format

# System Audit

## Report Metadata
Include performed date and timezone, repository, branch or commit, audit type, runtime evidence, exclusions, and comparison source.

## 1. Scope Reviewed
Explain files, services, environments, evidence, and exclusions. Record the branch or commit when available.

## 2. Executive Summary
Give a short plain-English summary of overall risk and the most urgent decisions.

## 3. Delta Since Previous Audit
When a prior report exists, classify each stable finding ID as:

- **Resolved:** acceptance criteria are verified
- **Improved:** risk or exposure is reduced but acceptance criteria are incomplete
- **Unchanged:** materially the same evidence and risk remain
- **Regressed:** exposure, severity, blast radius, or controls worsened
- **New:** not present or not observable in the prior scope
- **Not comparable:** scope or evidence changed materially

Do not mark a finding resolved because code changed; verify its acceptance criteria and tests.

## 4. Findings

For each finding, use:

### Finding: Short title
- ID: Stable finding identifier reused across reports
- Severity: Critical / High / Medium / Low
- Area: Security / Correctness / Reliability / Testing / Config / Docs
- Status: Confirmed / Inferred / Claimed / Unknown
- Longitudinal Status: Resolved / Improved / Unchanged / Regressed / New / Not comparable
- Evidence: File paths, functions, routes, config names, tests, or redacted runtime evidence
- Risk: What can go wrong
- Affected Scope: Users, tenants, data, services, or environments at risk
- Immediate Containment: Safe short-term action when needed
- Recommended Fix: What should change
- Acceptance Criteria: Evidence that demonstrates remediation
- Suggested Test: What test should be added or run

## 5. Positive Observations
Mention important controls that are already implemented correctly.

## 6. Recommended Fix Order
List what to address first and why.

## 7. Commands
Separate commands actually executed from commands recommended for later validation. Do not imply unexecuted commands passed.

## 8. Limitations and Open Questions
State missing runtime evidence, inaccessible environments, unresolved contradictions, and assumptions that could change findings.

## Analysis Plan Document

Write a separate plan containing:

1. Objective and risk decisions supported
2. Findings and unknowns requiring additional analysis
3. Recommended investigation and documentation activities in priority order
4. Evidence or stakeholder input needed for each activity
5. Expected document deliverable and acceptance criteria
6. Dependencies, risks, and suggested owner roles

Keep the plan non-executable: propose investigation, validation, and documentation work only. Do not apply fixes or modify code or configuration.

## Save the Analysis

1. Resolve the repository root with `git rev-parse --show-toplevel`; use the current workspace root when Git is unavailable.
2. Use the local calendar date in `MM-DD-YYYY` format.
3. Write the completed report to `docs/analysis/system-audit-MM-DD-YYYY.md`.
4. Write the separate plan to `docs/analysis/system-audit-plan-MM-DD-YYYY.md`.
5. Examples: `docs/analysis/system-audit-07-01-2026.md` and `docs/analysis/system-audit-plan-07-01-2026.md`.
6. Create `docs/analysis` when it does not exist; this is the only directory the skill may create.
7. Preserve every earlier dated report and plan. Do not edit or replace prior documents.
8. Do not overwrite an existing same-day report or plan. Report the collision and ask the user to approve alternate filenames.
9. Include the performed date and timezone, repository path, branch or commit when available, reviewed scope, evidence sources, assumptions, limitations, and comparison source.
10. Exclude plaintext credentials, tokens, PHI, sensitive request bodies, and unnecessary personal information.
11. Before finishing, verify both expected paths exist and each file is non-empty. State the two verified paths in the final response.
12. If the user explicitly requests a different output path or format, follow that instruction for both documents.

## Rules

- Do not exaggerate findings or claim a vulnerability without evidence.
- Re-verify current findings; do not merely copy the previous audit.
- Preserve prior reports as immutable measurement points.
- Reuse stable finding IDs so progress can be compared.
- Separate confirmed issues, inferences, claims, and unknowns.
- Distinguish repository evidence from deployed-state evidence.
- Explain severity and material uncertainty.
- Prefer practical fixes and measurable acceptance criteria.
- Do not run destructive, external, production, or state-changing commands without explicit approval.
- The only permitted writes are the two new documents required by this skill.
- Do not sign commits, PRs, or comments with an AI name.
