# PHI-Safe Logging Policy (New Code)

- **Date:** 2026-07-04
- **Scope:** Applies to new code going forward — currently `libs/llm_client`
  and any future caller of it or of `libs/safe_logging`. It is **not** a
  retroactive fix to existing services.

## This does not fix D1

`services/intake-service/app.py:65` logs the full intake request body
(name/DOB/SSN/notes) in plaintext at INFO — tracked as debt marker `D1`
(`docs/analysis/system-audit-07-01-2026.md` finding `AUD-06`, restated in
`docs/planning/ai-readiness-debt-log-07-04-2026.md`). This policy and its
helper do not touch that file. Fixing it is a separate, scoped decision, not
a side effect of adding a logging policy for unrelated new code — see
`CLAUDE.md`'s rule against opportunistically fixing documented debt.

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

This document does not assert that logging anywhere else in the system is
PHI-safe. It defines the policy for new code only.
