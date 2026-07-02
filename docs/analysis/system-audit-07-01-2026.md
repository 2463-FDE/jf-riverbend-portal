# System Audit

## Report Metadata

- **Performed:** 2026-07-01, America/New_York (EDT)
- **Repository:** `jf-riverbend-portal` (root: `/Users/jorge/Documents/Revature/jf-riverbend-portal`)
- **Branch / commit:** `jf-initial-analysis` @ `c9eb1e2`
- **Audit type:** Repository-only static/configuration audit. No live services were started; no external system (payer clearinghouse, hospital feed) was contacted; no database was queried at runtime.
- **Runtime evidence:** None beyond the actual filesystem state of `.env` (checked structurally — whether values differ from `.env.example` placeholders — without reading or printing any secret value) and `git ls-files`/`git log` on `.env`.
- **Exclusions:** `docs/temp` was not used as an evidence source (see Limitations). Dependency CVE scanning was not performed. The unit test suite was not executed (pytest is not installed in the available Python environment); test claims are based on direct reading of test source, not execution.
- **Comparison source:** None. This is the first system-audit report for this repository (`docs/analysis/` previously contained only `context-map-07-01-2026.md` and `context-map-plan-07-01-2026.md`).

## 1. Scope Reviewed

Whole repository: `frontend/`, all six domain services (`intake`, `eligibility`, `records`, `scheduling`, `interop`, `roi`) plus `gateway`, `db/schema.sql` and migrations, `config/roles.yaml`, `docker-compose.yml`, `.env`/`.env.example`, `.github/workflows/ci.yml`, `tests/` (all unit test files read directly, integration test file read but not executed), `adr/0001-0003`, `ARCHITECTURE.md`, `README.md`, `docs/handover/jira-tickets.md`, `docs/handover/auditor-questionnaire.md`.

Not reviewed / excluded: `docs/handover/portal.har` (not decoded), `docs/handover/breach-response-policy.md` and `docs/handover/payer-status-page.md` (referenced in the prior context map, not re-read in this pass), any live deployment environment (none exists to inspect — this repo's only environment is local Docker Compose), dependency CVE status of pinned package versions.

## 2. Executive Summary

This is a contractor handoff of a real healthcare intake/records system that is unusually candid about its own risk: the code itself contains explicit debt markers (`D1`, `D4`, `D5`, `D6`, `D8`, `D11`, `D12`), the ADRs name the same gaps, and the handover Jira board records real production incidents matching several of them. The most serious problems are not latent — they are already causing customer-visible harm and would fail a real compliance audit today: a real "auditor questionnaire" in the handover material shows staff unable to produce a disclosure accounting or an access log when asked (Q7/Q9), a Jira ticket (RIV-160) shows a clinician seeing an inconsistent allergy list for the same patient, and another (RIV-175) shows two patients double-booked into one appointment slot. Separately, every internal domain service accepts unauthenticated requests directly — the "gateway is the only entry point" trust boundary described in `ARCHITECTURE.md` is a convention, not an enforced control, and every service's port is also published to the host network.

The most urgent decision this audit supports: do not treat this system as production-ready or HIPAA-defensible as-is, regardless of the README's compliance claim. The four Critical findings below (network-bypassable services, IDOR, ROI/disclosure-accounting failure, silent clinical-data loss) each independently justify holding further clinical rollout until addressed or explicitly risk-accepted by someone with the authority to accept that risk.

## 3. Delta Since Previous Audit

Not applicable — first system-audit report for this repository.

## 4. Findings

### Finding: Internal domain services accept unauthenticated requests and are directly network-reachable
- ID: AUD-01
- Severity: Critical
- Area: Security
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/records-service/app.py`, `services/roi-service/app.py`, `services/intake-service/app.py`, `services/scheduling-service/app.py`, `services/interop-service/app.py`, `services/eligibility-service/app.py` — none of these define any authentication dependency; only `services/gateway/app.py`'s `require_session` checks a session, and only when a caller goes through the gateway. `docker-compose.yml` publishes every service's port to the host (`8071:8071` … `8076:8076`), not only the gateway's `8070:8070`.
- Risk: Anyone with network access to a domain service's port (same host, same LAN, or any route Docker's default bridge/port-publish makes reachable) can call it directly — e.g., `GET /patients/1042/records` on records-service, or `/roi/requests/{id}/fulfill` on roi-service — with **zero credentials**, bypassing login entirely. This is strictly worse than the IDOR finding below: it requires no session at all, valid or otherwise.
- Affected Scope: All patient PHI across all six services; all environments running this compose topology (per `ARCHITECTURE.md`, "production" is this same Docker Compose stack on a per-clinic VM).
- Immediate Containment: Restrict host-level firewall/network rules so only the gateway's port is reachable from outside the Docker network; do not expose `8071`-`8076` externally. This is a configuration/deployment mitigation, not a code change.
- Recommended Fix: Remove the `ports:` mappings for the six domain services in `docker-compose.yml` (they only need to be reachable from the gateway over the internal Docker network) and add real service-to-service authentication (signed JWT or mTLS) so a domain service rejects calls that didn't come from the gateway, independent of network placement.
- Acceptance Criteria: A direct HTTP request to any domain-service port from outside the Docker network fails to connect (port not published); a request that does reach a domain service without a valid service credential is rejected with 401/403.
- Suggested Test: An integration test that starts the compose stack and asserts a direct call to `http://localhost:8073/patients/1042/records` (bypassing the gateway) is rejected, not just that the gateway-mediated path requires a session.

### Finding: IDOR — chart reads are not scoped to the authenticated user's authorized patients
- ID: AUD-02
- Severity: Critical
- Area: Security
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/gateway/app.py` lines 136-140 (`proxy_records`, comment: "IDOR: ... never checked against {patient_id}"); `services/records-service/app.py` lines 86-98 (DEBT D11, "no ownership / authorization check"); `adr/0003-authentication-and-sessions.md` explicitly names this consequence. A test documenting the exact gap exists: `tests/integration/test_records_flow.py::test_user_cannot_read_other_patients_chart`, marked `xfail(strict=False)`.
- Risk: Any authenticated staff account (there is only one role — see AUD-13) can enumerate the sequential integer `patient_id` and read any patient's full chart, including clinical notes.
- Affected Scope: Every patient record in the system; every authenticated user (front desk, scheduler, ROI clerk) can read clinical data with no treatment relationship to the patient.
- Immediate Containment: None available without a code change — this is enforced nowhere in the current stack.
- Recommended Fix: Add a patient-scoped authorization check in the gateway's `require_session`-gated records routes (or in records-service itself) that verifies the caller's role/assignment against the requested `patient_id` before proxying/returning data.
- Acceptance Criteria: `test_user_cannot_read_other_patients_chart` passes without its `xfail` marker; a caller with no recorded relationship to a patient receives 403, not 200, on chart-read endpoints.
- Suggested Test: Convert the existing `xfail` integration test to a real assertion once fixed; add a records-service-level unit test for the authorization check independent of the gateway.

### Finding: ROI disclosures have no authorization enforcement and cannot produce a real accounting of disclosures
- ID: AUD-03
- Severity: Critical
- Area: Security / Compliance
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/roi-service/app.py` lines 83-170, explicitly labeled `DEBT D12`: no check for a signed 45 CFR 164.508 authorization before `fulfill_roi_request` releases records; no honoring of 164.522 restrictions; the `disclosures` row written has no `authorization_id`, no purpose, no restriction tracking (`db/schema.sql` lines 160-167, comment: "an accounting-of-disclosures cannot be produced"). Independently confirmed by `docs/handover/auditor-questionnaire.md`: a real payer pre-visit audit asked for an accounting of disclosures (Q7) and staff's written answer was "We don't have a way to pull that... audit_logs table is mostly request dumps."
- Risk: PHI can be released to any named recipient via `POST /roi/requests/{id}/fulfill` with no verification that a valid authorization exists, and the system cannot answer a real regulatory/auditor question about what was disclosed to whom. This is not a hypothetical — the exact scenario already happened.
- Affected Scope: Every ROI request; any patient whose records are subject to a release; Riverbend's standing with payers/auditors who ask this question.
- Immediate Containment: Require manual authorization verification and a separate paper/log record outside this system before calling `/roi/requests/{id}/fulfill` until the system enforces it itself.
- Recommended Fix: Add an `authorization_id`/purpose/restriction columns to `roi_requests`/`disclosures`, require a verified authorization reference before `fulfill_roi_request` proceeds, and make `disclosures` (or a dedicated table) capable of answering a 45 CFR 164.528 accounting-of-disclosures query directly.
- Acceptance Criteria: `fulfill_roi_request` rejects requests lacking a recorded authorization reference; a query exists that reproduces the exact answer the auditor asked for (all disclosures for patient X, to whom, under what authorization, in the last 6 years) directly from the data model.
- Suggested Test: A test asserting `fulfill_roi_request` returns an error when no authorization reference is present once the fix lands; a test that seeds several disclosures and asserts the accounting query returns the expected rows.

### Finding: Inbound HL7 parser silently drops allergy and medication data, compounding with duplicate-patient records
- ID: AUD-04
- Severity: Critical
- Area: Correctness / Patient Safety
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/interop-service/app.py` (module docstring: "intentionally brittle... silently drops AL1... and RXA... preserved here on purpose"), DEBT D6; `tests/test_hl7_parser.py::test_allergies_and_medications_are_captured` is `xfail(strict=True)`, confirming this is a known, currently-failing behavior with a locked-in regression guard (if it starts passing without the marker being removed, CI fails — a positive control, see Section 5). Real-world manifestation: `docs/handover/jira-tickets.md` RIV-160 — a clinician (Dr. Nguyen) observed the same patient's allergy list differ depending on which chart was opened, consistent with one chart populated via HL7 (allergy dropped) and another via direct/self-service intake (allergy preserved), compounded by the lack of a master-patient-index (AUD-09) creating multiple chart rows for one person in the first place.
- Risk: A clinician relying on a chart populated via the HL7 feed sees no error and no indication that allergy/medication data is missing — the field is simply empty, indistinguishable from "patient has no known allergies." This creates a real risk of administering a contraindicated medication.
- Affected Scope: Any patient whose data arrives via the hospital HL7 feed and who also has (or later gets) a duplicate chart from another intake path; potentially every patient ingested via `interop-service`, since the drop is unconditional, not duplicate-dependent.
- Immediate Containment: Treat any HL7-sourced chart's allergy/medication fields as unverified/incomplete in clinical workflow until the parser is fixed — this requires a process/communication change to clinical staff, not a code change, and should happen regardless of code-fix timeline.
- Recommended Fix: Extend `hl7_parser`'s segment map to include `AL1` and `RXA`, populate `encounters.allergies`/`medications` from them, and remove the `xfail` marker once verified.
- Acceptance Criteria: `test_allergies_and_medications_are_captured` passes; a chart populated via `/hl7/ingest` with a sample message containing AL1/RXA segments shows non-empty `allergies`/`medications` fields.
- Suggested Test: The existing xfail test, converted to a passing assertion, is sufficient; add a case with multiple AL1 segments (multiple allergies) to confirm the fix isn't a single-segment special case.

### Finding: Sessions never expire and there is no second factor
- ID: AUD-05
- Severity: High
- Area: Security
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/gateway/security.py` (`create_session`: "no expiry / TTL is set, so sessions never expire"), `services/gateway/auth.yaml` (`SESSION_TIMEOUT: never`, `mfa_enabled: false`), `adr/0003-authentication-and-sessions.md`.
- Risk: A leaked, stolen, or forgotten session token (e.g., left logged in on a shared clinical workstation, or exfiltrated via any client-side vulnerability) is valid forever, with no automatic logoff and no second factor to limit the blast radius.
- Affected Scope: Every logged-in session, i.e., all staff access to the system.
- Immediate Containment: Operationally instruct staff to explicitly log out on shared workstations; periodically flush all Redis session keys as a manual stopgap.
- Recommended Fix: Set a TTL on the Redis session key (e.g., sliding expiry on activity, absolute cap) and add session revocation on password change; MFA is a reasonable fast-follow (also flagged by ADR 0003 against the 2025 HIPAA Security Rule NPRM).
- Acceptance Criteria: A session created via `/login` expires automatically after the configured idle/absolute window; `GET /me` with an expired token returns 401.
- Suggested Test: Unit test on `create_session`/`get_session` asserting a TTL is set on the Redis key; integration test asserting an artificially-expired session is rejected.

### Finding: PHI is written in full, in plaintext, to an application log file
- ID: AUD-06
- Severity: High
- Area: Security / Config
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/intake-service/app.py` line 65 (`log.info('POST /intake body=%s', req.model_dump_json())`, labeled `DEBT D1`); `services/intake-service/logging_config.py` (writes to `logs/intake-service.log`, a repo-relative file with no documented access control or rotation/retention policy visible in the codebase).
- Risk: The intake request body includes name, DOB, SSN, and free-text clinical notes. Anyone with read access to `logs/intake-service.log` — a much broader and less-monitored surface than the database — obtains PHI in the clear.
- Affected Scope: Every patient registered via `/intake` since this logging line was introduced; anyone with filesystem or log-aggregation access.
- Immediate Containment: Redact or truncate the logged payload immediately (log a patient ID or a hash instead of the full body) without waiting for a broader logging overhaul.
- Recommended Fix: Replace the full-body log with a structured, PHI-free log line (event type, patient ID, elapsed time — which is already logged separately at line 82); add a repo-wide policy/lint check that flags `model_dump_json()` (or equivalent) passed to a logger call.
- Acceptance Criteria: `logs/intake-service.log` after a fix contains no SSN/DOB/name/notes fields for new requests; existing log files are rotated/purged per a documented retention decision (a decision this audit does not make).
- Suggested Test: A test that captures log output from `create_intake` and asserts no PHI field (by name) appears in any log record.

### Finding: `audit_logs` is a mutable, soft-deletable table, not a tamper-evident access trail
- ID: AUD-07
- Severity: High
- Area: Security / Compliance
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `db/schema.sql` lines 125-136 (`audit_logs` — comment: "Ordinary mutable table; rows can be UPDATE/DELETEd and soft-deleted... This is logging, not tamper-evident auditing"); `adr/0002-data-and-compliance.md` ("'Audit' is effectively request logging and is mutable"). Independently confirmed by `docs/handover/auditor-questionnaire.md` Q9 ("List everyone who viewed patient Y in the last 90 days") — staff's answer did not include any such capability.
- Risk: There is no reliable way to answer "who accessed this patient's data and when," which is both an operational blind spot (can't investigate a suspected IDOR exploitation, for example) and a compliance failure demonstrated against a real question.
- Affected Scope: Every patient; any future security incident investigation or compliance audit.
- Immediate Containment: None effective without a schema/process change — mutability is structural.
- Recommended Fix: Make access/audit logging append-only (revoke UPDATE/DELETE from the application's DB role on this table, or move to a write-once store), and ensure every PHI read (not just writes) is captured with actor, patient, action, and timestamp — which nothing currently does for reads.
- Acceptance Criteria: The application DB role cannot UPDATE or DELETE `audit_logs` rows (verified via a privilege check); a query can answer "who viewed patient Y in the last 90 days" directly.
- Suggested Test: A DB-level test attempting an UPDATE/DELETE as the app role and asserting it is rejected; an integration test asserting a chart-read produces a corresponding access-log row.

### Finding: Real credentials are committed to git, not just present in the working tree
- ID: AUD-08
- Severity: High
- Area: Security / Config
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `.env` is tracked (`git ls-files .env` returns it) and has been present since the initial scaffold commit (`b9364ca`) and touched again in `d0905a1`. Structural comparison (values, not printed) confirms `DB_PASSWORD`, `PAYER_API_KEY`, and `SESSION_SECRET` in `.env` differ from the `changeme` placeholders in `.env.example` — i.e., real-looking working values, not just placeholders, are in git history. `.gitignore` does not list `.env`.
- Risk: Anyone who has ever cloned this repository (including this handoff itself) has these credentials, and they remain in git history even if `.env` were removed from the working tree today without a history rewrite.
- Affected Scope: Database access, the payer API key, and session-token signing — all three secrets in this file.
- Immediate Containment: Rotate all three credentials now, independent of any code fix, since they must be treated as already disclosed to every past and present holder of this repository.
- Recommended Fix: Remove `.env` from version control (`git rm --cached .env`, add it to `.gitignore`), rotate the credentials, and adopt a secrets-injection mechanism (vault, deploy-time env injection) so no real credential is ever committed again. A history rewrite (e.g., `git filter-repo`) is a separate, higher-risk decision this audit does not recommend unilaterally.
- Acceptance Criteria: `.env` is no longer tracked; `.gitignore` includes it; the specific credential values found in history have been rotated and no longer work.
- Suggested Test: A CI secret-scanning step (see AUD-16) that fails the build if a future commit reintroduces a tracked `.env` or an embedded secret pattern.

### Finding: No master-patient-index — self-service/front-desk intake creates duplicate patient charts
- ID: AUD-09
- Severity: High
- Area: Correctness
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `db/schema.sql` lines 42-43 ("no unique match key on (name, dob, ssn)... forks one person into several rows"), `services/intake-service/app.py` (`DEBT D5`, "every `/intake` creates a brand new patients row, so one person forks into several charts"), `intake.yaml` (`match_key: none`, per code comment). Real-world manifestation: RIV-160 (see AUD-04) shows this occurring for an actual patient (Maria Gonzalez).
- Risk: Fragmented clinical history across multiple chart rows for the same person — compounds the HL7 allergy-loss finding (AUD-04) into a directly observed clinical-safety incident, and separately risks duplicate/incorrect billing and insurance association.
- Affected Scope: Any patient registered more than once via any combination of self-service, front-desk, or HL7-sourced intake.
- Immediate Containment: Front-desk process control — search for an existing patient by name/DOB before registering a new one (a workaround, not a fix, and error-prone).
- Recommended Fix: Add a deterministic match-key check (e.g., name + DOB + last-4-SSN) at intake that flags likely duplicates for staff confirmation before creating a new `patients` row, per the existing `intake.yaml` config hook (`match_key`) already present but set to `none`.
- Acceptance Criteria: Submitting a second intake with matching name/DOB/SSN either merges into the existing patient or requires explicit staff confirmation to proceed as a new patient.
- Suggested Test: An intake-service test asserting a second `/intake` call with identical demographics does not silently create a second `patients` row (or triggers a documented confirmation path).

### Finding: The only automated test for the IDOR vulnerability never runs in CI
- ID: AUD-10
- Severity: High
- Area: Testing
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `tests/integration/test_records_flow.py` (contains `test_user_cannot_read_other_patients_chart`, the IDOR-documenting xfail test, and the only tests asserting `GET /patients/{id}/records` requires authentication at all) is marked `pytestmark = pytest.mark.integration`; `.github/workflows/ci.yml` runs `pytest -m "not integration"` — the integration suite, and everything in it, is excluded from every CI run on every push/PR.
- Risk: Not only does IDOR (AUD-02) lack a passing regression test — the entire class of "does authentication even apply to this route" tests never executes automatically. A future change that accidentally removed `require_session` from a route would not be caught by CI.
- Affected Scope: Every route whose auth-requirement coverage lives only in the integration suite; currently records, login, and IDOR-adjacent behavior.
- Immediate Containment: Run `pytest -m integration` manually against a local `make up` stack before merging any change touching authentication or records routes, until CI covers it.
- Recommended Fix: Add a CI job that stands up Postgres + Redis (e.g., via service containers in the GitHub Actions workflow) and runs the integration suite, or at minimum extracts the auth-requirement assertions into unit-testable form (e.g., testing the FastAPI dependency directly, as `test_gateway_security.py` already does for password hashing) so they don't require live infrastructure.
- Acceptance Criteria: A CI run demonstrably executes `test_records_require_authentication` and `test_user_cannot_read_other_patients_chart` (or their unit-test equivalents) and the pipeline reports their outcome.
- Suggested Test: N/A — this finding is itself about the absence of a CI-run test; the fix is the test infrastructure change described above.

### Finding: Synchronous, no-timeout payer eligibility call blocks patient intake — confirmed production incident
- ID: AUD-11
- Severity: High
- Area: Reliability
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/eligibility-service/check.py` line 17 (`requests.get(...)` with no `timeout=`), `services/intake-service/app.py` lines 138-154 (`DEBT D4`, hardcoded `time.sleep(4.2)` plus a further no-timeout `httpx.get` call to eligibility-service, "BLOCKS the request thread by design"). Confirmed in production: `docs/handover/jira-tickets.md` RIV-088 (registration spins 4-5s every time) and RIV-141 ("Tue 9:00-9:20am the ENTIRE intake screen froze... not just eligibility").
- Risk: A slow or unresponsive payer endpoint has already frozen all patient registration for 20 minutes in production, not hypothetically.
- Affected Scope: Every patient registration; front-desk operations clinic-wide during a payer outage.
- Immediate Containment: None available without a code or infrastructure change; a payer-side outage today reproduces RIV-141 exactly.
- Recommended Fix: Add a bounded timeout and retry/circuit-breaker to the payer HTTP call; decouple eligibility verification from the synchronous `/intake` request (e.g., return `pending` immediately and verify asynchronously via a background worker or job queue), matching the intent already implied by DEBT D4's comment ("the cohort's fix is to make this async / bounded").
- Acceptance Criteria: A hung payer endpoint causes the eligibility check to fail fast (bounded time) without blocking patient chart creation; `/intake` completes in bounded time regardless of payer latency.
- Suggested Test: A test using a mocked payer endpoint that hangs indefinitely, asserting `/intake` still completes within a bounded time budget.

### Finding: Scheduling has a confirmed check-then-insert double-booking race
- ID: AUD-14
- Severity: High
- Area: Correctness
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/scheduling-service/book.py` (`slot_taken()` then `insert_appointment()`, explicit comment: "classic check-then-act race... no UNIQUE constraint on slot_id and no idempotency key"); `db/schema.sql` line 79 (`slot_id INTEGER NOT NULL, -- NOTE: no UNIQUE constraint, no FK`). Confirmed in production: RIV-175 ("two people showed up for the same slot once").
- Risk: Concurrent or retried booking requests for the same slot both succeed, double-booking a patient appointment — already observed with a real patient-facing impact (two people arriving for one slot).
- Affected Scope: Any appointment slot booked under concurrent or retried requests; clinic scheduling operations.
- Immediate Containment: None effective without a schema or locking change — the race window exists on every booking call.
- Recommended Fix: Add a `UNIQUE` constraint on `(slot_id)` where status is 'confirmed' (or a partial unique index), and/or use `SELECT ... FOR UPDATE` locking around the check-then-insert, plus an idempotency key on the booking endpoint to make client retries safe.
- Acceptance Criteria: Two concurrent booking requests for the same slot result in exactly one confirmed appointment and one rejected/conflict response.
- Suggested Test: A concurrency test issuing simultaneous `book()` calls for the same `slot_id` and asserting only one succeeds (currently untested per `tests/README.md`, which explicitly notes this gap).

### Finding: README's HIPAA-compliance and encryption claims contradict the system's actual data-protection posture
- ID: AUD-12
- Severity: High
- Area: Docs
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `README.md` line 1 ("All PHI is encrypted and the system is fully HIPAA compliant") and its "Compliance" section ("All patient data is encrypted..."), directly contradicted by `adr/0002-data-and-compliance.md` ("PHI columns... are stored as plain TEXT... We do not add application-level or column-level encryption") and `ARCHITECTURE.md` §7 ("Compliance posture is self-asserted... PHI columns are plaintext").
- Risk: Anyone relying on the README at face value — a new engineer, a business associate, an auditor doing a document review — is given a materially false statement about the system's data protection, which itself is a compliance and reputational exposure independent of the underlying technical gaps.
- Affected Scope: Anyone consuming `README.md` as a source of truth, including future contractors, auditors, or business partners.
- Immediate Containment: None needed — this is a documentation correction, not a running-system risk, but should not wait for the underlying technical fixes.
- Recommended Fix: Correct `README.md` to accurately describe the current posture (storage-layer encryption + TLS in transit only, no column-level encryption, no independent HIPAA certification) and remove the unqualified "fully HIPAA compliant" claim until it is actually true and verified by a qualified party.
- Acceptance Criteria: `README.md` and `ADR 0002`/`ARCHITECTURE.md` no longer contradict each other on encryption and compliance posture.
- Suggested Test: N/A — documentation fix; verify by reading the corrected README against the ADRs.

### Finding: Every account has a single flat role — no least-privilege / minimum-necessary access
- ID: AUD-13
- Severity: Medium
- Area: Security
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `config/roles.yaml` ("Everyone who works here gets the 'staff' role. Simple to manage" — single role grants `patients.read/write`, `records.read/write` including clinical notes, `billing.read`, `disclosures.read`, `appointments.write`); `db/schema.sql` (`users.role` comment: "single role for everyone"); `adr/0003` ("No least-privilege... minimum-necessary access... is not enforced").
- Risk: A scheduler or ROI clerk account has the same clinical-notes read/write access as a clinician; there is no data-layer enforcement of minimum-necessary access as HIPAA's minimum-necessary standard expects. This is a lower severity than AUD-02 (IDOR) because even a correctly-scoped role system wouldn't fully close that gap, but it's a real, independent weakness.
- Affected Scope: Every account; every functional persona (front desk, clinician, scheduler, ROI clerk) has more access than their job requires.
- Immediate Containment: None practical without a code/config change.
- Recommended Fix: Split `staff` into role-scoped permissions matching the four documented personas (front-desk, clinician, scheduler, roi-clerk), each with only the permissions their function needs.
- Acceptance Criteria: A front-desk account cannot access an endpoint gated to `records.write`-only functionality (e.g., clinical note authoring), verified by an authorization test per role.
- Suggested Test: A parameterized test asserting each role can/cannot reach each gateway route per an explicit permission matrix.

### Finding: All services share a single Postgres credential — no per-service least privilege
- ID: AUD-15
- Severity: Medium
- Area: Config
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `docker-compose.yml` (`env_file: .env` on every service, one `POSTGRES_USER`/`DB_USER` — `riverbend_app` — for all of them); `adr/0001-monorepo-and-stack.md` names this as deferred ("no per-service least-privilege DB users").
- Risk: A vulnerability or bug in any one service (e.g., the unauthenticated network exposure in AUD-01) grants effectively full database access, not just access to that service's owned tables.
- Affected Scope: All data in Postgres, via any single compromised service.
- Immediate Containment: None practical without a config/schema change.
- Recommended Fix: Create per-service Postgres roles with `GRANT`s limited to the tables each service actually owns (per the ownership table in `ARCHITECTURE.md` §2).
- Acceptance Criteria: `records-service`'s DB role cannot write to `roi_requests`/`disclosures`, and equivalent restrictions hold for each other service, verified by attempting an out-of-scope query under each service's credential.
- Suggested Test: A DB-permission test run per service asserting denial on tables outside that service's ownership.

### Finding: CI has no dependency, container image, or secret scanning, and builds straight from `main`
- ID: AUD-16
- Severity: Medium
- Area: Config
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `.github/workflows/ci.yml` — explicit comment: "NOTE: no dependency vulnerability scan, no container image scan, and no secret-scanning step. Images are built and pushed straight from main." The `services` CI job is only a per-service Python **import** smoke test (`python -c "import app"`), not a real test suite.
- Risk: A vulnerable dependency, a leaked secret in a future commit (which already happened once, per AUD-08), or an insecure base image would not be caught before merge/build.
- Affected Scope: Every future change to this repository.
- Immediate Containment: Manual review discipline for dependency updates and secret handling until automated scanning exists.
- Recommended Fix: Add a dependency-vulnerability scan (e.g., `pip-audit`/`npm audit` as CI steps), a container image scan, and a secret-scanning step (e.g., gitleaks) to `.github/workflows/ci.yml`.
- Acceptance Criteria: CI fails on a known-vulnerable dependency, a detectable secret pattern, or a flagged base-image CVE in a test PR crafted to trigger each check.
- Suggested Test: N/A — CI configuration change; validate by intentionally introducing a detectable test violation in a throwaway branch (not performed in this audit).

### Finding: Unindexed full-table scan and N+1 query pattern in records access
- ID: AUD-17
- Severity: Low
- Area: Reliability
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `services/records-service/app.py` lines 86-134 (`DEBT D8`, N+1 — one query per encounter) and lines 137-161 (`DEBT D8`, "full-table ILIKE scan on records.body with NO supporting index and NO result limit... deliberate debt").
- Risk: Both patterns scale poorly; currently low-impact given demo data volume (~690 records per `README.md`), but will degrade with real clinical data volume, and the unbounded search has no result cap at all.
- Affected Scope: `GET /patients/{id}/records` and `GET /records/search` response times as data grows.
- Immediate Containment: None needed at current data volume.
- Recommended Fix: Use a join/`selectinload` for the chart assembly; add a supporting index (e.g., trigram/GIN) and a result limit to `records/search`.
- Acceptance Criteria: Chart assembly issues a bounded number of queries regardless of encounter count; search enforces a maximum result count.
- Suggested Test: A query-count assertion (e.g., via SQLAlchemy event listener) on chart assembly; a test asserting `records/search` caps results.

### Finding: Declared HL7 feed configuration is unused/dead
- ID: AUD-18
- Severity: Low
- Area: Config
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `.env.example` declares `HL7_FEED_HOST`/`HL7_FEED_PORT`; no code path in `services/interop-service` or elsewhere consumes them — the actual mechanism is inbound `POST /hl7/ingest`.
- Risk: Low direct risk, but misleading — anyone reading `.env.example` reasonably infers an active outbound/listening HL7 connection exists, which it does not.
- Affected Scope: Anyone configuring a deployment based on `.env.example`.
- Immediate Containment: None needed.
- Recommended Fix: Either remove the unused variables, or (if a real-time listener is actually planned) document the gap between declared config and current behavior. This is a product/roadmap question this audit does not resolve unilaterally (see companion analysis plan).
- Acceptance Criteria: `.env.example` accurately reflects what is actually consumed by the codebase.
- Suggested Test: N/A — configuration/documentation cleanup.

### Finding: No lint/type-check gate in CI beyond an implicit `next build`; per-service CI check is an import smoke test only
- ID: AUD-19
- Severity: Low
- Area: Testing
- Status: Confirmed
- Longitudinal Status: New
- Evidence: `.github/workflows/ci.yml` `services` job runs `python -c "import app; print(...)"` per service — this only confirms the module imports without error, not that it behaves correctly; `frontend` job runs `npm run build` only, no explicit `npm run lint` step despite `lint` existing in `frontend/package.json`.
- Risk: A change that imports cleanly but is behaviorally broken, or a lint violation, would not be caught by this CI job in isolation (the separate `tests` job does provide some real coverage, per AUD-10's caveat about integration tests).
- Affected Scope: Any change to service or frontend code.
- Immediate Containment: None needed.
- Recommended Fix: Add `npm run lint` as an explicit CI step; consider the import-smoke-test job as a fast pre-check only, not a substitute for the `tests` job it runs alongside.
- Acceptance Criteria: CI fails on a linting violation in the frontend.
- Suggested Test: N/A — CI configuration change.

## 5. Positive Observations

- Passwords are hashed with PBKDF2-SHA256 (260,000 iterations), a per-hash random salt, and constant-time comparison (`hmac.compare_digest`) in `services/gateway/security.py` — this specific mechanism is sound, independent of the session-expiry weakness (AUD-05).
- SQL access throughout the reviewed services uses parameterized queries (SQLAlchemy ORM `select()`/`.ilike()`, or psycopg2 `%s` placeholders in `book.py`) — no string-interpolated SQL was found, so no SQL-injection finding is reported.
- Each service validates its own request/response shape via Pydantic v2 schemas (`schemas.py` per service), providing real input validation at the service boundary even though the gateway itself forwards payloads as opaque `dict`s.
- The codebase is unusually transparent about its own debt: explicit `DEBT D#` comments, an `xfail(strict=True)` test (`test_hl7_parser.py`) that will fail the build if the known AL1/RXA-drop bug is silently "fixed" without updating the test — a genuine regression-in-reverse guard — and a Jira ticket trail that independently corroborates several code-level findings. This makes evidence-based auditing of this repository unusually tractable.
- `docker-compose.yml` uses proper healthchecks and `depends_on: condition: service_healthy` ordering, reducing startup-race flakiness in local/demo environments.
- Demo/seed data is generated deterministically (`db/seed/generate_seed.py`) rather than hand-maintained, reducing drift risk in test fixtures.

## 6. Recommended Fix Order

1. **AUD-08** (rotate committed secrets) — zero-code-change, immediate, and every other fix's credibility depends on not operating with already-disclosed credentials.
2. **AUD-01** (network-level auth bypass) — closes the most severe hole first; every other authorization fix (AUD-02, AUD-03, AUD-13) is moot if a caller can skip the gateway entirely.
3. **AUD-02** and **AUD-03** (IDOR; ROI authorization/accounting) — the two confirmed, highest-blast-radius PHI-exposure paths once network bypass is closed.
4. **AUD-04** and **AUD-09** (HL7 data loss; duplicate patients) — patient-safety cluster; fix together since they compound in the observed RIV-160 incident.
5. **AUD-05**, **AUD-13**, **AUD-15** (session expiry/MFA, role granularity, DB least-privilege) — the authorization/access-control hardening cluster.
6. **AUD-06**, **AUD-07** (PHI in logs; mutable audit trail) — data-protection cluster, needed to make AUD-03's fix meaningful (an accounting is only as good as the underlying access trail).
7. **AUD-11**, **AUD-14** (synchronous payer call; double-booking) — both are confirmed production incidents with direct operational/patient impact; fix once security-critical items are addressed.
8. **AUD-10**, **AUD-16**, **AUD-19** (CI/testing gaps) — close these alongside the fixes above so each fix ships with a regression test that actually runs.
9. **AUD-12** (README correction) — trivial to do at any point; do it as soon as convenient rather than leaving a false public claim in place while other fixes land.
10. **AUD-17**, **AUD-18** (performance debt; dead config) — lowest priority, no urgency at current scale.

## 7. Commands

**Executed in this audit** (all read-only or local-environment-only; no external system, production environment, or live credential was used):
```
git rev-parse --show-toplevel
git branch --show-current
git rev-parse --short HEAD
git ls-files .env
git log --oneline -- .env
diff <(grep -o '^[A-Z_]*=' .env.example | sort) <(grep -o '^[A-Z_]*=' .env | sort)
# structural placeholder-vs-real-value check on DB_PASSWORD / PAYER_API_KEY / SESSION_SECRET,
# comparing whether values differ from .env.example — no secret value was printed or logged
ls -la .env
python3 -m pytest -m "not integration" -q   # failed: pytest not installed in the available environment; not executed
```

**Recommended for later validation (not executed in this audit):**
```
make test                                    # run the unit suite for real (installs requirements-dev.txt first)
make up && pytest -m integration             # exercises the IDOR xfail and auth-required tests live — only after
                                              # reviewing AUD-08 (rotate secrets first; .env holds real values)
pip-audit -r requirements-dev.txt            # and per-service requirements.txt — dependency CVE scan, not run here
docker compose config -q                     # (make config) — compose file validation
```

## 8. Limitations and Open Questions

- `docs/temp` was deliberately excluded as an audit evidence source per this session's scoping (it is a scenario/roleplay document, not architecture or audit input); the one overlapping factual claim it made (PHI in logs) is independently confirmed here via AUD-06 from source code directly.
- The unit test suite was not executed (no `pytest` in the available Python environment) — test-behavior claims in this report (e.g., which tests currently pass, the exact xfail semantics) are based on direct reading of test source and marker arguments, not a live test run. Recommend running `make test` to convert this from Confirmed-by-reading to Confirmed-by-execution.
- No dependency, container, or infrastructure vulnerability scan was performed — Section 7's recommended commands include this as unexecuted follow-up.
- The real production/deployment environment (a VM per clinic region, per `ARCHITECTURE.md`) was not available for inspection; all findings reflect the repository and its Docker Compose definition, which may or may not match how a live clinic VM is actually configured (e.g., whether host firewall rules already mitigate AUD-01 in practice — unknown).
- Whether `HL7_FEED_HOST`/`HL7_FEED_PORT` (AUD-18) reflect a planned integration or stale config could not be resolved from repository evidence alone.
- This report is a repository/configuration audit, not a penetration test, legal opinion, or HIPAA compliance certification, and should not be represented as one.
