"""Structured, safe counter emission for golden-signal metrics.

Week 7's gap was never a missing dashboard/alert format — it was that no
metric anywhere in this repo was actually produced, so any dashboard/alert
written against one would be inert. This module does not stand up a
monitoring platform, an exporter, or a new dependency: a counter increment is
emitted as one structured, greppable log line via `libs.safe_logging`, in a
stable `metric=<name> value=<n> key=value ...` shape. Any log-based metrics
system already deployable in this environment (CloudWatch Logs Insights, a
self-hosted ELK/Loki stack, or a plain `grep`/`wc -l` during a demo) can count
these lines today. See
docs/planning/policy-navigator-golden-signals-week7-08-25-2026.md for the
dashboard/alert spec written against this exact line shape.

Labels are plain metric dimensions only, redacted defense-in-depth like every
other telemetry helper in this repo (libs.tracing, libs.agent_provenance) —
never a prompt, response, retrieved text, or patient identifier. Never
raises: a broken logger must not fail the request being measured, mirroring
libs.tracing.spans's own no-op-on-failure contract.
"""
from libs.safe_logging import get_safe_logger
from libs.safe_logging.redact import redact

log = get_safe_logger(__name__)


def record_counter(name: str, value: int = 1, **labels) -> None:
    """Emit one counter increment as a structured, safe log line.

    `name` should be a stable, versionless metric name (e.g.
    `policy_navigator_termination_total`). `labels` are metric dimensions —
    plain metadata only.
    """
    try:
        safe_labels = redact(labels)
        rendered = " ".join(f"{key}={safe_labels[key]}" for key in sorted(safe_labels))
        log.info("metric emitted: metric=%s value=%s %s", name, value, rendered)
    except Exception as exc:  # instrumentation must never fail the caller
        try:
            log.warning("metric emission failed (error_type=%s)", type(exc).__name__)
        except Exception:  # the fallback log call itself must be best-effort too
            pass
