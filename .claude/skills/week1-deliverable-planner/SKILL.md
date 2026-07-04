---
name: weekly-deliverable-planner
description: Use this skill when planning a weekly client deliverable from system-audit, context-map, baseline-architecture docs, and a new client request. It should produce a safe commit plan before modifying files.
---

# Weekly Deliverable Planner

## Purpose

Plan a safe weekly implementation from the project documentation and client request.

This skill is for brownfield work where the client asks for something broad or risky, but the actual weekly deliverable is narrower and must protect security, PHI, reliability, and project scope.

## Required Inputs

Before changing files, inspect:

- `docs/analysis/system-audit-*`
- `docs/analysis/context-map-*`
- `docs/analysis/baseline-architecture-*`
- current client request
- weekly deliverable instructions
- relevant source files only after the plan is clear

## Rules

- You may read and inspect any files inside this project freely.
- You may search the repository, documentation, source code, tests, configs, and CI files as needed.
- Do not write, edit, delete, move, or format any files until I approve the plan.
- I will create branches and commits myself unless I explicitly ask you to do it.
- Do not run `git commit`, `git push`, or create branches.
- After planning, output the proposed PR title and PR description on screen.
- Do not build the client-requested AI assistant unless the weekly deliverable explicitly asks for it.
- Do not send PHI or real patient data to any external model.
- Do not hardcode, print, commit, or place API keys in prompts.
- Do not edit `.env` directly.
- Use placeholder values only in `.env.example`.
- Keep the LLM client provider-swappable.
- OpenAI may be used only through environment configuration such as `OPENAI_API_KEY`.
- Automated tests must use fake provider responses or mocks, not real OpenAI calls.

## Planning Output

First produce an implementation plan using exactly 4 commits:

### Commit 1: `docs: add onboarding seam map and AI risk debt log`

Include:

- 1-page onboarding seam map
- debt-log entry naming D1, D9, and D3 in business-risk terms
- no code changes

### Commit 2: `feat(llm): add production LLM client wrapper`

Include:

- provider-swappable LLM client structure
- timeout handling
- retry with exponential backoff and jitter
- structured-output parsing
- token/cost guard
- OpenAI provider only through environment/config, never hardcoded

### Commit 3: `feat(logging): add PHI-safe logging policy and redaction helper`

Include:

- no-request-body logging policy
- redaction helper for sensitive fields
- no API keys, raw prompts, raw request bodies, PHI, or model responses with patient data in logs

### Commit 4: `test: add LLM client and PHI-safe logging tests`

Include tests for:

- retry/backoff behavior
- timeout handling
- structured-output parsing
- token/cost guard
- redaction/no-PHI logging
- mocked/fake model calls only

Also inspect `.github/workflows/ci.yml`:

- If the existing `pytest -m "not integration" -q` command already runs the new tests, do not modify CI unnecessarily.

- If new test dependencies are needed, update the appropriate requirements file so CI installs them.

- If the new tests need a new command or marker adjustment, update CI in this commit.

- Do not add any CI step that requires a real OpenAI API key.

- Do not call the real OpenAI API in CI.

- CI should validate the wrapper and logging behavior using fake providers or mocks only.

## Workflow

1. Inspect the repository and documentation freely.
2. Summarize relevant findings from the docs and code.
3. Explain why the client’s requested AI assistant should not be built yet if it conflicts with the weekly deliverable.
4. Propose exact the 4-commit plan.
5. Output a proposed PR title and PR description on screen.
6. Wait for approval before changing files.
7. After approval, implement one commit-sized section at a time.
8. Do not create branches, commit, or push. I will handle Git.
9. After each commit, show:
   - files changed
   - tests run
   - remaining work