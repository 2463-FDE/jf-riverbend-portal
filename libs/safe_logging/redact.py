"""Redaction helper for sensitive/PHI-shaped fields in structured (dict/list)
log data.

This is a defense-in-depth backstop for structured metadata, not a
substitute for the primary control: never pass a raw prompt, raw model
response, raw request/response body, or a secret to a logger call in the
first place. See docs/planning/phi-safe-logging-policy.md.
"""
from typing import Any, Mapping

REDACTED = "***REDACTED***"

# Field names treated as sensitive wherever they appear in a dict passed to
# the logger, case-insensitively. Deliberately broad — a false positive (an
# unrelated field happening to be named "notes") is cheap; a false negative
# (a PHI field slipping through) is not.
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "ssn",
        "social_security_number",
        "dob",
        "date_of_birth",
        "name",
        "first_name",
        "last_name",
        "patient_name",
        "full_name",
        "address",
        "street_address",
        "phone",
        "phone_number",
        "email",
        "notes",
        "clinical_notes",
        "diagnosis",
        "allergies",
        "medications",
        "prompt",
        "response",
        "completion",
        "raw_body",
        "body",
        "api_key",
        "authorization",
        "token",
        "session_token",
        "password",
    }
)


def redact(payload: Any) -> Any:
    """Recursively redact known-sensitive keys in a dict/list structure.

    Returns a new structure; never mutates the input. Values that are
    neither a dict nor a list are returned unchanged — there is no field
    name to match against a bare string or number.
    """
    if isinstance(payload, Mapping):
        return {
            key: (REDACTED if str(key).lower() in SENSITIVE_FIELD_NAMES else redact(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload
