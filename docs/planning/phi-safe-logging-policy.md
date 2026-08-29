# PHI-Safe Logging Policy

- **Date:** 2026-07-04. **Updated 2026-08-29 (W10 Final Stage 3):** originally
  scoped to new code only (`libs/llm_client` and any future caller of
  `libs/safe_logging`); every repository service's `logging_config.py` now
  attaches `PHISafeFilter` (rule 6) and every `log.exception(...)` call
  across `services/` was replaced with categorical `type(exc).__name__`
  logging (rule 5) — see the rules below for what that closes and does not.

## D1 status

`services/intake-service/app.py:65` used to log the full intake request body
(name/DOB/SSN/notes) in plaintext at INFO — tracked as debt marker `D1`
(`docs/analysis/system-audit-07-01-2026.md` finding `AUD-06`, restated in
`docs/planning/ai-readiness-debt-log-07-04-2026.md`). **Resolved separately**
(Week 1 catch-up) — `app.py`'s `_intake_log_summary` now logs only an
allowlist (`correlation_id`, `created_via`), never the request body; see that
file's own module docstring for the full history. Not touched by this policy
or by W10 Final Stage 3.

## Rules

1. **Never log a raw prompt.** Prompt text sent to an LLM provider is
   arbitrary caller-supplied content and must be treated as if it might
   contain PHI, even in a codebase that today only sends synthetic/test
   input to a model.
2. **Never log a raw model response.** Same reasoning in reverse — a model's
   output can echo back or summarize whatever was in its input.
3. **Never log a raw request or response body object** (a dict, a Pydantic
   model dump, a raw HTTP body) at any log level. Log field-limited,
   structured metadata instead: event name, provider, attempt number,
   elapsed time, aggregate token counts, outcome.
4. **Never log an API key, session token, or other secret** — not even
   truncated or partially masked. A masked-but-still-present secret in a log
   line is still a secret in a log line.
5. **Never log a raw exception message from a third-party SDK or from
   parsing model output.** Log the exception's **type name only**
   (`type(exc).__name__`). Rationale: a validation error message (e.g. from
   parsing structured output) can echo back the invalid input it was given —
   which, for this client, is model response text that may contain patient
   data. A provider SDK's own error message can likewise echo request
   parameters. The type name is sufficient for operational triage without
   this risk.
6. **Use `libs.safe_logging.get_safe_logger(name)`** for any new logger
   in new code. It attaches `PHISafeFilter`, which redacts known-sensitive
   dict/list-shaped log arguments as a backstop — this is defense-in-depth,
   not a substitute for rules 1-5.
7. **If structured metadata of unknown/variable shape must be logged**
   (e.g. a config dict), pass it through `libs.safe_logging.redact()`
   explicitly before logging, in addition to the filter backstop.

## What the redaction helper does and does not do

`redact()` is a field-name-based backstop for **structured** data (dicts and
lists) — it does not scan free text for PHI-shaped substrings. There is no
reliable regex/keyword pass that can guarantee removal of arbitrary PHI from
free-form text such as a prompt or a model response. That is exactly why
rules 1 and 2 exist: raw prompt/response text must never reach a logger call
in the first place, rather than being logged and then "cleaned."

## Non-goal

This document does not assert that every log line in the system is PHI-safe.
Rules 6 (safe filter attached) and 5 (categorical exception logging) now
apply repository-wide (W10 Final Stage 3); rules 1-4 and 7 remain a
discipline enforced at each call site, not something a filter can guarantee
— a future call that logs a raw body/prompt/response would still violate
this policy even with the filter attached.
