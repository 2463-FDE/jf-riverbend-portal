"""A logging.Filter that redacts known-sensitive structured fields before a
record reaches any handler (console, file, aggregator, etc.).

Defense-in-depth only — see docs/planning/phi-safe-logging-policy.md for the
primary control (never pass raw prompts/responses/bodies/secrets to a logger
call in the first place).
"""
import logging

from .redact import redact


class PHISafeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):
            record.args = redact(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(
                redact(arg) if isinstance(arg, (dict, list)) else arg for arg in record.args
            )

        if isinstance(record.msg, (dict, list)):
            record.msg = redact(record.msg)

        return True
