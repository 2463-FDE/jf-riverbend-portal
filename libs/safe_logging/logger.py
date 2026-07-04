"""A minimal, PHI-safe logger factory for new code.

Deliberately separate from each service's own copy-pasted logging_config.py
(see adr/0001) — this is new shared infrastructure for new code (currently
libs/llm_client), not a replacement for any existing service's logging.
"""
import logging
import os

from .filters import PHISafeFilter


def get_safe_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if any(isinstance(f, PHISafeFilter) for f in logger.filters):
        return logger

    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.addFilter(PHISafeFilter())

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logger.addHandler(handler)

    return logger
