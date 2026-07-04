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
These are NOT oversights to "fix" in the test suite; they mirror real gaps:
- **No tests for the scheduling race / double-booking** (`book.py`). The happy
  path is exercised manually only.
- **No tests asserting IDOR is prevented** — there's an `xfail` documenting that
  cross-patient reads currently succeed (they shouldn't).
- **HL7 allergy/medication extraction is `xfail`** — the parser silently drops
  AL1/RXA; the test documents the gap rather than hiding it.
- **No tests for ROI authorization enforcement** — none exists to test.
- **No tests for input normalization / duplicate-patient prevention.**
- Security/auth path coverage overall is thin (tracked as RIV-201).
