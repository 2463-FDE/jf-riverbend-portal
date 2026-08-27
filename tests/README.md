# Tests

```bash
pip install -r ../requirements-dev.txt
pytest -m "not integration"     # unit tests, no infra needed
pytest -m integration           # needs `make up` (Postgres + Redis + stack)
```

There is no shared Python package across services (adr/0001), so unit tests load
the module under test by file path (see `conftest.py::load_module`). The one
exception is `libs/` (currently `libs.llm_client`, `libs.safe_logging`), which
*is* a real shared package and is imported normally — `conftest.py` puts the
repo root on `sys.path` for this.

## What's covered
- `test_gateway_security.py` — password hashing/verification roundtrip + edge cases.
- `test_hl7_parser.py` — HL7 PID/PV1 happy path.
- `test_eligibility_check.py` — payer eligibility response shaping.
- `test_intake_schemas.py` — multi-step intake payload validation.
- `test_llm_client.py` — LLM client retry/backoff, timeout handling, structured-output
  parsing, token/cost guard, and PHI-safe logging behavior, all against `FakeProvider`
  (no real provider calls or API keys).
- `test_safe_logging.py` — redaction helper and logging filter/factory in `libs.safe_logging`.
- `integration/test_records_flow.py` — login + auth-gating + chart read.

## Known coverage gaps (deliberate — this is an inherited codebase)
These are NOT oversights to "fix" in the test suite; they mirror real gaps.
Several were true at handoff and have since been closed in later catch-up
work — corrected below to match current tests, not the handoff snapshot.
- ~~**No tests for the scheduling race / double-booking** (`book.py`). The
  happy path is exercised manually only.~~ **Resolved** (Week 5 catch-up,
  RIV-175) — `integration/test_scheduling_concurrency.py` exercises concurrent
  booking replay/conflict directly against `book.py`.
- ~~**No tests asserting IDOR is prevented** — there's an `xfail` documenting
  that cross-patient reads currently succeed (they shouldn't).~~ **Resolved**
  (Week 4 catch-up, RIV-201) —
  `integration/test_records_flow.py::test_user_cannot_read_other_patients_chart`
  is a real, passing regression test (403 for an ungranted chart), not an
  xfail.
- **HL7 allergy/medication extraction is `xfail`** — the parser silently drops
  AL1/RXA; the test documents the gap rather than hiding it. Still open.
- **No tests for ROI authorization enforcement** — none exists to test. Still
  open.
- **No tests for input normalization / duplicate-patient prevention** —
  **partially covered now**:
  `test_registration_user_can_review_the_seeded_duplicate_cluster` in
  `integration/test_records_flow.py` exercises the reconciliation view over the
  seeded Maria Gonzalez duplicate cluster, but there is still no test directly
  exercising `_find_match_candidates`' exact/partial match-blocking logic in
  `services/intake-service/app.py` itself. Input normalization coverage
  remains absent.
- Security/auth path coverage has improved (tracked as RIV-201): IDOR is
  regression-tested, and `test_gateway_rbac.py` proves each least-privilege
  role is both denied a permission the legacy flat `staff` role granted and
  can still reach its own permitted routes. Two real gaps remain: no test
  exercises a staff account actually migrated off `staff` (none exist yet —
  that migration is gated on the client's roster), and `test_gateway_rbac.py`
  covers gateway-route gating only. There is **no** test of role enforcement
  at the data-query boundary, because that enforcement doesn't exist yet —
  `records-service` consults no role at all today. That is this cycle's
  primary RBAC work, and it is where the enforcement test must live.
- ~~No MFA tests: the TOTP prototype and its tests are parked, unmerged, on
  `feat/mfa-totp-parked`.~~ **Resolved** (w8-planner-2, PR #101):
  `test_gateway_mfa_*.py`, `test_gateway_login_route.py`, and
  `test_gateway_security_mfa.py` cover enrollment, the login challenge,
  backup codes, supervisor reset, rollout-mode/pilot-scope behavior, and
  that no secret material reaches logs or `audit_logs`.
