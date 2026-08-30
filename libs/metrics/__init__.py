"""Minimal, safe golden-signal counter emission — see counters.py for the
full contract; a log-line counter, no new dependency. `http.py` (W10 Final
Stage 6) is a separate, real Prometheus registry for scrapeable HTTP
request metrics — import it directly (`libs.metrics.http`), not re-exported
here, since it's a real new dependency (prometheus-client) the four
in-scope services opt into, not every consumer of this package.
"""
from .counters import record_counter

__all__ = ["record_counter"]
