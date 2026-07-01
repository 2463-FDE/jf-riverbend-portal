---
name: system-audit
description: Use this skill when auditing a repository, service, feature, or pull request for security, correctness, reliability, testing gaps, configuration risks, and production-readiness issues.
---

# System Audit Skill

## Purpose

Use this skill to inspect the system for risks and gaps.

The goal is to identify:

- Security issues
- Authorization/authentication gaps
- Correctness bugs
- Missing tests
- Reliability risks
- Configuration problems
- Production-readiness concerns
- Documentation gaps

Do not modify files unless the user explicitly asks for fixes.

## Process

1. Understand the relevant scope first.
2. Inspect code, configuration, routes, tests, and documentation.
3. Look for security and correctness issues.
4. Check whether behavior is enforced on the backend, not only the frontend.
5. Check whether tests cover important success and failure paths.
6. Check whether configuration and environment variables are safe.
7. Prioritize findings by severity and impact.
8. Recommend concrete fixes.

## Audit Areas

Review these areas when relevant:

- Authentication and authorization
- Input validation
- IDOR and tenant/user isolation
- Secrets and environment variables
- Logging of sensitive data
- Error handling
- Retry logic and timeout behavior
- Rate limiting and abuse protection
- Database constraints and migrations
- Race conditions and idempotency
- API gateway behavior
- Frontend-only security assumptions
- Test coverage
- CI/CD checks
- Docker and infrastructure exposure
- Documentation accuracy

## Output Format

Use this format:

# System Audit

## 1. Scope Reviewed
Explain what files, services, or features were reviewed.

## 2. Executive Summary
Give a short plain-English summary of the overall risk.

## 3. Findings

For each finding, use:

### Finding: Short title
- Severity: Critical / High / Medium / Low
- Area: Security / Correctness / Reliability / Testing / Config / Docs
- Evidence: File paths, functions, routes, or config names
- Risk: What can go wrong
- Recommended Fix: What should change
- Suggested Test: What test should be added or run

## 4. Positive Observations
Mention important things that are already done correctly.

## 5. Recommended Fix Order
List what to fix first, second, third.

## 6. Commands to Run
List validation commands such as tests, lint, build, or Docker commands.

## Rules

- Do not exaggerate findings.
- Do not claim a vulnerability without evidence.
- Separate confirmed issues from possible risks.
- Prefer practical fixes over theoretical advice.
- Do not make code changes unless asked.
- Do not sign commits, PRs, or comments with an AI name.