"""Shared-ish logging setup (copy-pasted per service — see ADR 0001).

W10 Final Stage 3, corrected post-review (LOG-FILTER-PROPAGATION): a filter
attached only to this service's own logger object never runs for a record a
CHILD logger (e.g. `<service_name>.worker`) propagates up — Python's
logging module only consults the HANDLERS along the propagation path, never
an ancestor logger's own `.filters`. PHISafeFilter (see
docs/planning/phi-safe-logging-policy.md) is therefore attached to every
handler on the root logger `basicConfig` configures, so it runs for any
logger in this process that reaches those handlers. The service-named
logger also keeps its own filter as defense in depth — harmless, but not by
itself sufficient.
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
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, PHISafeFilter) for f in handler.filters):
            handler.addFilter(PHISafeFilter())
    return logger
