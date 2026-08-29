"""Shared-ish logging setup (copy-pasted per service — see ADR 0001).

W10 Final Stage 3: attaches libs.safe_logging's PHISafeFilter (already a
real shared package, unlike this copy-pasted module — see ADR 0001 and
docs/planning/phi-safe-logging-policy.md) to every logger this factory
returns. Defense-in-depth only: it redacts known-sensitive dict/list-shaped
log arguments — never a substitute for keeping raw PHI out of a log call
in the first place.
"""
import logging
import os

from libs.safe_logging import PHISafeFilter


def configure(service_name: str) -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [" + service_name + "] %(message)s",
    )
    logger = logging.getLogger(service_name)
    if not any(isinstance(f, PHISafeFilter) for f in logger.filters):
        logger.addFilter(PHISafeFilter())
    return logger
