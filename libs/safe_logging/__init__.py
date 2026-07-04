"""PHI-safe logging helpers: a redaction function for structured data, a
logging.Filter backstop, and a logger factory for new code.

See docs/planning/phi-safe-logging-policy.md for the policy this implements.
"""
from .filters import PHISafeFilter
from .logger import get_safe_logger
from .redact import REDACTED, SENSITIVE_FIELD_NAMES, redact

__all__ = [
    "redact",
    "REDACTED",
    "SENSITIVE_FIELD_NAMES",
    "PHISafeFilter",
    "get_safe_logger",
]
