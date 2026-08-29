"""Logging setup for intake-service (copy-pasted per service — see ADR 0001).

This module only wires up handlers (console + repo-level
logs/intake-service.log); it does not decide what gets logged. What used to be
a real gap here — app.py logging the full intake request body, PHI included,
at INFO — was fixed across several review rounds (DEBT D1; see app.py's module
docstring for the full history). app.py now logs only an allowlisted summary
(_intake_log_summary: correlation_id, created_via), never the request body.
Keep any future logging call in app.py to that same allowlist discipline; this
module has no way to enforce it.

W10 Final Stage 3, corrected post-review (LOG-FILTER-PROPAGATION): a filter
attached only to this logger object never runs for a record a CHILD logger
propagates up to it — only the HANDLERS along the propagation path are
consulted. PHISafeFilter (docs/planning/phi-safe-logging-policy.md) is
attached to both the console and file handlers below, not just the logger.
"""
import logging
import os

from libs.safe_logging import PHISafeFilter


def _ensure_filter(target) -> None:
    if not any(isinstance(f, PHISafeFilter) for f in target.filters):
        target.addFilter(PHISafeFilter())


def configure(service_name: str) -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level, logging.INFO)

    logger = logging.getLogger(service_name)
    logger.setLevel(log_level)
    _ensure_filter(logger)  # defense in depth — see module docstring

    # Don't stack duplicate handlers if configure() is called more than once,
    # but still make sure whatever handlers already exist carry the filter.
    if logger.handlers:
        for handler in logger.handlers:
            _ensure_filter(handler)
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s [" + service_name + "] %(message)s")

    # Console handler.
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    _ensure_filter(stream)
    logger.addHandler(stream)

    # File handler — repo-level logs/<service>.log. Create the directory robustly
    # so the container does not crash at startup on a fresh volume.
    logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
    os.makedirs(logs_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(logs_dir, service_name + ".log"))
    file_handler.setFormatter(fmt)
    _ensure_filter(file_handler)
    logger.addHandler(file_handler)

    return logger
