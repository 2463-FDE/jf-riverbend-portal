"""Tests for the metadata-only OTel wrapper (libs/tracing).

No real `opentelemetry` install is required for these — the "not installed"
path is exercised directly (this repo's CI never installs it, per the Stage 3
hard rule), and the "installed" path is exercised via a fake `opentelemetry`
module registered in sys.modules, mirroring tests/test_bedrock_provider.py's
established fake-SDK pattern.
"""
import logging
import sys
import types

import pytest

from libs.tracing import new_correlation_id, record_event, safe_span
from libs.tracing import spans as tracing_spans


@pytest.fixture(autouse=True)
def _reset_provider_configured_flag():
    # _configure_tracer_provider() is a one-shot-per-process guard; reset it
    # so each test's fake SDK stack (or lack thereof) is evaluated fresh.
    tracing_spans._provider_configured = False
    yield
    tracing_spans._provider_configured = False


def test_correlation_id_is_a_safe_opaque_string():
    a = new_correlation_id()
    b = new_correlation_id()

    assert a != b
    assert len(a) == 32  # uuid4().hex
    assert all(c in "0123456789abcdef" for c in a)


def test_safe_span_without_otel_installed_yields_a_working_noop(monkeypatch):
    # Ensure a prior test's fake `opentelemetry` module can't leak in.
    monkeypatch.delitem(sys.modules, "opentelemetry", raising=False)
    monkeypatch.delitem(sys.modules, "opentelemetry.trace", raising=False)

    with safe_span("svc", "op", {"job_id": "abc123", "status": "queued"}) as span:
        span.set_attribute("extra", "value")
        record_event(span, "queued", {"retry_count": 0})
    # No exception is the test — nothing above should raise even though
    # opentelemetry isn't installed.


def test_safe_span_never_swallows_the_wrapped_codes_own_exception():
    with pytest.raises(ValueError):
        with safe_span("svc", "op") as span:
            span.set_attribute("k", "v")
            raise ValueError("business logic failure")


def test_safe_span_attributes_are_redacted_before_reaching_a_real_span(monkeypatch):
    captured = {}

    class _FakeSpan:
        def set_attribute(self, key, value):
            captured[key] = value

        def add_event(self, name, attributes=None):
            captured[f"event:{name}"] = attributes

        def set_status(self, status):
            pass

    class _FakeSpanCM:
        def __enter__(self):
            return _FakeSpan()

        def __exit__(self, *a):
            return False

    class _FakeTracer:
        def start_as_current_span(self, name):
            return _FakeSpanCM()

    fake_trace_mod = types.ModuleType("opentelemetry.trace")
    fake_trace_mod.get_tracer = lambda name: _FakeTracer()
    fake_trace_mod.Status = lambda code: code
    fake_trace_mod.StatusCode = types.SimpleNamespace(OK="OK", ERROR="ERROR")
    fake_otel_mod = types.ModuleType("opentelemetry")
    fake_otel_mod.trace = fake_trace_mod

    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace_mod)

    with safe_span("svc", "op", {"ssn": "111-22-3333", "job_id": "job-1"}) as span:
        record_event(span, "checked", {"ssn": "111-22-3333", "status": "active"})

    assert captured["ssn"] == "***REDACTED***"
    assert captured["job_id"] == "job-1"
    assert captured["event:checked"]["ssn"] == "***REDACTED***"
    assert captured["event:checked"]["status"] == "active"


def test_span_start_failure_degrades_to_noop_and_does_not_raise(monkeypatch, caplog):
    class _ExplodingTracer:
        def start_as_current_span(self, name):
            raise RuntimeError("exporter unreachable")

    fake_trace_mod = types.ModuleType("opentelemetry.trace")
    fake_trace_mod.get_tracer = lambda name: _ExplodingTracer()
    fake_otel_mod = types.ModuleType("opentelemetry")
    fake_otel_mod.trace = fake_trace_mod

    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace_mod)

    with caplog.at_level(logging.WARNING):
        with safe_span("svc", "op") as span:
            span.set_attribute("k", "v")  # must be the no-op, not raise

    assert any("degrading to no-op" in r.getMessage() for r in caplog.records)
    assert "exporter unreachable" not in "\n".join(r.getMessage() for r in caplog.records)


def _install_fake_otel_sdk(monkeypatch, *, tracer_factory, set_tracer_provider):
    """Registers a fake opentelemetry + opentelemetry.sdk.* stack, enough to
    drive _configure_tracer_provider's real control flow."""
    fake_trace_mod = types.ModuleType("opentelemetry.trace")
    fake_trace_mod.get_tracer = tracer_factory
    fake_trace_mod.set_tracer_provider = set_tracer_provider
    fake_trace_mod.Status = lambda code: code
    fake_trace_mod.StatusCode = types.SimpleNamespace(OK="OK", ERROR="ERROR")
    fake_otel_mod = types.ModuleType("opentelemetry")
    fake_otel_mod.trace = fake_trace_mod

    fake_resources_mod = types.ModuleType("opentelemetry.sdk.resources")
    fake_resources_mod.Resource = types.SimpleNamespace(create=lambda attrs: attrs)

    class _FakeTracerProvider:
        def __init__(self, resource=None):
            self.resource = resource
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    fake_sdk_trace_mod = types.ModuleType("opentelemetry.sdk.trace")
    fake_sdk_trace_mod.TracerProvider = _FakeTracerProvider

    class _FakeBatchSpanProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class _FakeConsoleSpanExporter:
        pass

    fake_export_mod = types.ModuleType("opentelemetry.sdk.trace.export")
    fake_export_mod.BatchSpanProcessor = _FakeBatchSpanProcessor
    fake_export_mod.ConsoleSpanExporter = _FakeConsoleSpanExporter

    fake_sdk_mod = types.ModuleType("opentelemetry.sdk")

    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk", fake_sdk_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.resources", fake_resources_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", fake_sdk_trace_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace.export", fake_export_mod)
    return _FakeConsoleSpanExporter


def test_provider_is_configured_with_a_console_exporter_when_no_otlp_endpoint_is_set(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    calls = {"provider": None}

    class _FakeSpanCM:
        def __enter__(self):
            return types.SimpleNamespace(set_attribute=lambda *a, **k: None, set_status=lambda *a, **k: None)

        def __exit__(self, *a):
            return False

    class _FakeTracer:
        def start_as_current_span(self, name):
            return _FakeSpanCM()

    console_exporter_cls = _install_fake_otel_sdk(
        monkeypatch,
        tracer_factory=lambda name: _FakeTracer(),
        set_tracer_provider=lambda provider: calls.__setitem__("provider", provider),
    )

    with safe_span("svc", "op"):
        pass

    assert calls["provider"] is not None
    assert isinstance(calls["provider"].processors[0].exporter, console_exporter_cls)


def test_provider_setup_is_attempted_at_most_once_per_process(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    attempts = {"n": 0}

    class _FakeSpanCM:
        def __enter__(self):
            return types.SimpleNamespace(set_attribute=lambda *a, **k: None, set_status=lambda *a, **k: None)

        def __exit__(self, *a):
            return False

    class _FakeTracer:
        def start_as_current_span(self, name):
            return _FakeSpanCM()

    def _set_tracer_provider(provider):
        attempts["n"] += 1

    _install_fake_otel_sdk(
        monkeypatch, tracer_factory=lambda name: _FakeTracer(), set_tracer_provider=_set_tracer_provider
    )

    with safe_span("svc", "op1"):
        pass
    with safe_span("svc", "op2"):
        pass

    assert attempts["n"] == 1


def test_missing_otlp_exporter_package_falls_back_to_console_without_crashing(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example.test:4318")
    calls = {"provider": None}

    class _FakeSpanCM:
        def __enter__(self):
            return types.SimpleNamespace(set_attribute=lambda *a, **k: None, set_status=lambda *a, **k: None)

        def __exit__(self, *a):
            return False

    class _FakeTracer:
        def start_as_current_span(self, name):
            return _FakeSpanCM()

    console_exporter_cls = _install_fake_otel_sdk(
        monkeypatch,
        tracer_factory=lambda name: _FakeTracer(),
        set_tracer_provider=lambda provider: calls.__setitem__("provider", provider),
    )
    # No opentelemetry.exporter.otlp.* module is registered, so the lazy
    # OTLP import inside _configure_tracer_provider raises ModuleNotFoundError
    # — must degrade to the console exporter, never crash span creation.

    with safe_span("svc", "op"):
        pass

    assert calls["provider"] is not None
    assert isinstance(calls["provider"].processors[0].exporter, console_exporter_cls)
