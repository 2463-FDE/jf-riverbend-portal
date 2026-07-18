"""Metadata-only OpenTelemetry helpers for Stage 3.

Spans/events carry correlation IDs and outcome/status metadata only — never
a prompt, model response, request/tool payload, member ID, payer payload, or
secret. See docs/planning/phi-safe-logging-policy.md for the same rule
applied to logging; this is the tracing equivalent.

OpenTelemetry is an optional dependency: the Stage 3 hard rule is "OTel deps
go in the consuming service's requirements only", never requirements-dev.txt.
Every `opentelemetry` import here is lazy (mirrors
libs/llm_client/providers/bedrock_provider.py's lazy `import boto3`), so
importing this module never requires the SDK installed. A service that
hasn't installed/configured it transparently gets a no-op tracer instead of
an ImportError, and an exporter/collector outage degrades to a no-op rather
than failing the request being instrumented.
"""
from .spans import new_correlation_id, record_event, safe_span

__all__ = ["safe_span", "record_event", "new_correlation_id"]
