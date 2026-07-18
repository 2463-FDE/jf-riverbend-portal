"""Span/event helpers — see package docstring for the metadata-only contract.

`safe_span` is the only entry point that opens a span; `record_event` attaches
a metadata-only event to one already open. Both degrade to a no-op on any
underlying OpenTelemetry failure (not installed, not configured, exporter
unreachable) and never raise for that reason — only the wrapped code's own
exceptions propagate, exactly like a plain `with` block would without tracing
at all.
"""
import os
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

from libs.safe_logging import get_safe_logger
from libs.safe_logging.redact import redact

log = get_safe_logger(__name__)

_provider_configured = False


def _configure_tracer_provider() -> None:
    """Set up a real TracerProvider + exporter exactly once per process.

    Without this, `trace.get_tracer(...)` returns the OpenTelemetry API's
    default no-op provider even with opentelemetry-sdk installed — spans
    would be created and silently discarded, never actually exported
    anywhere, which would make "non-fatal exporter failures" a meaningless
    requirement (there'd be no exporter to fail). Exporter selection:
    `OTEL_EXPORTER_OTLP_ENDPOINT` if set (its exporter package is imported
    lazily and best-effort — falls back to console if that package isn't
    installed), otherwise `ConsoleSpanExporter` (part of opentelemetry-sdk
    itself, no extra package, always available once the SDK is).

    Attempted at most once per process (mirrors this repo's read-config-
    once-at-process-start convention — config.py's module-level `settings =
    Settings()`); any failure here is logged (TYPE only) and left as a
    no-op provider rather than raised.
    """
    global _provider_configured
    if _provider_configured:
        return
    _provider_configured = True
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        exporter = None
        otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            except Exception as exc:
                log.warning(
                    "OTLP exporter unavailable, falling back to console (error_type=%s)",
                    type(exc).__name__,
                )
        if exporter is None:
            exporter = ConsoleSpanExporter()

        service_name = os.getenv("OTEL_SERVICE_NAME", "riverbend-service")
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception as exc:
        log.warning("tracing provider setup failed, spans will be no-op (error_type=%s)", type(exc).__name__)


def new_correlation_id() -> str:
    """A safe, opaque, non-guessable correlation id for tying spans/logs
    together across services — never derived from PHI or any caller-supplied
    identifier (member ID, patient ID, etc.)."""
    return uuid.uuid4().hex


class _NoOpSpan:
    def set_attribute(self, key, value):
        pass

    def add_event(self, name, attributes=None):
        pass

    def record_exception_type(self, error_type: str):
        pass

    def set_status_ok(self):
        pass

    def set_status_error(self):
        pass


class _RealSpan:
    def __init__(self, otel_span):
        self._span = otel_span

    def set_attribute(self, key, value):
        try:
            self._span.set_attribute(key, value)
        except Exception as exc:  # exporter/SDK failures must never propagate
            log.warning("tracing set_attribute failed (error_type=%s)", type(exc).__name__)

    def add_event(self, name, attributes=None):
        try:
            self._span.add_event(name, attributes=redact(attributes or {}))
        except Exception as exc:
            log.warning("tracing add_event failed (error_type=%s)", type(exc).__name__)

    def record_exception_type(self, error_type: str):
        self.set_attribute("error.type", error_type)

    def set_status_ok(self):
        try:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            log.warning("tracing set_status failed (error_type=%s)", type(exc).__name__)

    def set_status_error(self):
        try:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_status(Status(StatusCode.ERROR))
        except Exception as exc:
            log.warning("tracing set_status failed (error_type=%s)", type(exc).__name__)


def _get_otel_tracer(tracer_name: str):
    """Returns a real OTel tracer, or None if the SDK isn't installed or
    tracing raises for any other reason. Never raises itself."""
    try:
        from opentelemetry import trace

        _configure_tracer_provider()
        return trace.get_tracer(tracer_name)
    except Exception as exc:
        log.warning("tracing unavailable, using no-op (error_type=%s)", type(exc).__name__)
        return None


@contextmanager
def safe_span(tracer_name: str, span_name: str, attributes: Optional[dict] = None) -> Iterator[object]:
    """Start a metadata-only span, degrading to a no-op on any tracing
    failure. Always yields a span-like object exposing set_attribute/
    add_event/record_exception_type — callers don't need to branch on
    whether tracing is actually active.

    `attributes` must be plain metadata (ids, statuses, counts, durations) —
    never a prompt, response, request/tool body, member ID, payer payload, or
    secret. Values are redacted defense-in-depth via the same field-name
    backstop libs.safe_logging uses for structured log data; that is not a
    substitute for the caller never passing PHI/secrets in the first place.
    """
    safe_attributes = redact(attributes or {})
    otel_tracer = _get_otel_tracer(tracer_name)

    span_cm = None
    wrapped: object = _NoOpSpan()
    if otel_tracer is not None:
        try:
            span_cm = otel_tracer.start_as_current_span(span_name)
            otel_span = span_cm.__enter__()
            wrapped = _RealSpan(otel_span)
            for key, value in safe_attributes.items():
                wrapped.set_attribute(key, value)
        except Exception as exc:
            log.warning("tracing span start failed, degrading to no-op (error_type=%s)", type(exc).__name__)
            span_cm = None
            wrapped = _NoOpSpan()

    try:
        yield wrapped
    except Exception as exc:
        wrapped.record_exception_type(type(exc).__name__)
        wrapped.set_status_error()
        raise
    else:
        wrapped.set_status_ok()
    finally:
        if span_cm is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception as exc:
                log.warning("tracing span end failed (error_type=%s)", type(exc).__name__)


def record_event(span: object, name: str, attributes: Optional[dict] = None) -> None:
    """Attach a metadata-only event to an already-open span (real or no-op).
    Never raises."""
    add_event = getattr(span, "add_event", None)
    if add_event is None:
        return
    try:
        add_event(name, redact(attributes or {}))
    except Exception as exc:
        log.warning("tracing record_event failed (error_type=%s)", type(exc).__name__)
