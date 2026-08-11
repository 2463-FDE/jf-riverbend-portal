"""Logging setup for intake-service (copy-pasted per service — see ADR 0001).

This module only wires up handlers (console + repo-level
logs/intake-service.log); it does not decide what gets logged. What used to be
a real gap here — app.py logging the full intake request body, PHI included,
at INFO — was fixed across several review rounds (DEBT D1; see app.py's module
docstring for the full history). app.py now logs only an allowlisted summary
(_intake_log_summary: correlation_id, created_via), never the request body.
Keep any future logging call in app.py to that same allowlist discipline; this
module has no way to enforce it.
"""
import logging
import os


def configure(service_name: str) -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level, logging.INFO)

    logger = logging.getLogger(service_name)
    logger.setLevel(log_level)

    # Don't stack duplicate handlers if configure() is called more than once.
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s [" + service_name + "] %(message)s")

    # Console handler.
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    # File handler — repo-level logs/<service>.log. Create the directory robustly
    # so the container does not crash at startup on a fresh volume.
    logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
    os.makedirs(logs_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(logs_dir, service_name + ".log"))
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
