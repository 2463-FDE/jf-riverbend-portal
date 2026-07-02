# System Audit Analysis Plan

Companion plan to `docs/analysis/system-audit-07-01-2026.md`. This document proposes further investigation, validation, and documentation work only — it does not apply any fix or modify code or configuration.

## 1. Objective and Risk Decisions Supported

Resolve the audit's remaining unknowns and unexecuted validations so that whoever prioritizes remediation (see the audit's Section 6 fix order) is deciding from confirmed evidence rather than static-analysis inference alone — particularly whether AUD-01 (network-bypassable services) is already mitigated by real deployment network rules, and whether the unit/integration test suites actually pass as their source code implies.

## 2. Findings and Unknowns Requiring Additional Analysis

1. Whether the actual production/clinic-VM deployment already restricts network access to domain-service ports (AUD-01's blast radius depends entirely on this; the repo's `docker-compose.yml` alone doesn't answer it).
2. Whether the unit test suite (`pytest -m "not integration"`) actually passes end-to-end when executed (not run in this audit — no `pytest` available in the audit environment).
3. Whether the integration suite (`pytest -m integration`), once run against a live `make up` stack, confirms the exact behavior described in AUD-02/AUD-10 (the IDOR xfail, the auth-required tests).
4. Whether any dependency in `requirements-dev.txt` or any per-service `requirements.txt` / `frontend/package-lock.json` has a known CVE (not assessed in this audit).
5. Whether `HL7_FEED_HOST`/`HL7_FEED_PORT` (AUD-18) reflect a real planned integration or stale config — carried over from the companion context-map's open question.
6. The actual current values of the three real secrets confirmed present in `.env` (AUD-08) need rotation, and this plan intentionally does not attempt that itself (out of scope for a documentation/analysis skill; requires credential-owner action).

## 3. Recommended Investigation Activities (priority order)

1. **Rotate the credentials confirmed exposed in AUD-08** (Critical priority, outside this skill's scope to perform). This is the single highest-value, lowest-effort action available and should not wait on any other finding.
2. **Confirm real deployment network exposure for AUD-01** (High priority). Whoever manages the per-clinic VM should confirm whether ports `8071`-`8076` are actually reachable from outside the Docker bridge network in the real environment, not just per `docker-compose.yml`. This directly changes AUD-01's real-world severity from "confirmed at the compose-file level" to either "confirmed exploitable in production" or "mitigated by existing network controls, but still worth removing at the compose-file level as defense-in-depth."
3. **Execute the unit test suite for real** (High priority, low effort). Install `requirements-dev.txt` in a clean environment and run `make test`, to convert every test-source-based claim in AUD-04's xfail evidence and AUD-10's CI-scope finding from "confirmed by reading source" to "confirmed by execution."
4. **Execute the integration suite against a local `make up` stack, after rotating secrets** (Medium priority — do this after Activity 1, since `.env`'s real values would otherwise be used to start the stack). Confirms the exact current behavior of `test_user_cannot_read_other_patients_chart` and the auth-required tests live, not just from source.
5. **Run a dependency-vulnerability scan** (Medium priority). `pip-audit` against each service's `requirements.txt` and `requirements-dev.txt`, and `npm audit` against `frontend/package-lock.json` — neither was run in this audit.
6. **Resolve the HL7 feed config question** (Low priority, carried from the context map's open question — same activity, not duplicated effort).

## 4. Evidence or Stakeholder Input Needed per Activity

| Activity | Evidence / input needed |
|---|---|
| 1. Rotate secrets | Whoever holds credential-management authority for the DB, payer API key, and session-secret signing (likely outside the engineering team alone) |
| 2. Confirm real network exposure | Access to (or a report from) whoever manages the actual clinic-VM deployment's firewall/network configuration — not found in this repository |
| 3. Execute unit tests | A Python environment with `pip install -r requirements-dev.txt` permitted (not available in the audit's sandboxed environment) |
| 4. Execute integration tests | A local `make up` stack, run only after Activity 1 (secret rotation) to avoid exercising real credentials |
| 5. Dependency scan | `pip-audit`/`npm audit` tooling and network access to fetch CVE databases (not available/authorized in this audit's read-only pass) |
| 6. HL7 feed intent | Riverbend/hospital-IT stakeholder conversation (same as the companion context-map plan's Activity 2 — do not duplicate, just confirm once) |

## 5. Expected Document Deliverable and Acceptance Criteria

- Activities 2-5 should each update the corresponding finding's **Status** field (Confirmed by reading → Confirmed by execution/live evidence) in a future dated `system-audit-MM-DD-YYYY.md` revision, using the same finding IDs (AUD-01, AUD-04, AUD-10, etc.) so longitudinal comparison in Section 3 of the next report is meaningful.
- Activity 1 (secret rotation) should be confirmed by whoever performs it, with a note added to a future audit's Section 3 marking AUD-08 **Resolved** once rotation is verified independently (not merely claimed).
- Activity 5 should produce a short dependency-scan output artifact (kept outside this repo if it would otherwise expose more detail than desired) referenced by finding ID AUD-16 in a future audit.
- Activity 6 is shared with the companion context-map plan — one resolution serves both documents; do not commission it twice.

## 6. Dependencies, Risks, and Suggested Owner Roles

- **Dependencies:** Activity 4 depends on Activity 1 completing first (do not run the integration suite against a stack still using the currently-exposed real credentials without rotating them, or without explicit risk acceptance from whoever owns that decision).
- **Risks:** Running the integration suite (Activity 4) starts real services with real database connections — even locally, this should use rotated (or newly-generated dev-only) credentials, not the ones already confirmed exposed in git history, to avoid further normalizing their use.
- **Suggested owner roles:** Activity 1 — whoever holds credential/secrets-management authority at Riverbend or its hosting provider. Activity 2 — whoever manages the clinic-VM deployment/network (not identified in this repository — see the companion context-map's ownership gap). Activities 3, 4, 5 — engineering (whoever picks up this handoff), ideally in a CI environment rather than manually, per AUD-10/AUD-16's recommended fixes. Activity 6 — shared with the context-map plan, likely Riverbend IT/ops.
