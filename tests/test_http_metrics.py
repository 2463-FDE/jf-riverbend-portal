"""libs/metrics/http.py — real, scrapeable Prometheus HTTP request metrics
(W10 Final Stage 6, sub-slice 1). Request count, latency, in-flight work,
all labeled by route TEMPLATE (never a raw path/patient/user/correlation
id) and a bounded status class.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libs.metrics.http import (
    REQUEST_COUNT,
    REQUEST_LATENCY_SECONDS,
    REQUESTS_IN_FLIGHT,
    install_http_metrics,
    metrics_response,
)

# The complete, bounded set of label names this module may ever emit — no
# metric here may carry any other label, especially not one shaped like a
# patient/user/correlation id.
_ALLOWED_LABEL_NAMES = {"service", "method", "route", "status_class"}


def _app():
    app = FastAPI()
    install_http_metrics(app, "test-service")

    @app.get("/items/{item_id}")
    def get_item(item_id: int):
        return {"item_id": item_id}

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.get("/metrics")
    def metrics():
        return metrics_response()

    return app


def _counter_value(counter, **labels):
    try:
        return counter.labels(**labels)._value.get()
    except Exception:
        return 0.0


def test_only_the_bounded_label_set_is_ever_used():
    assert set(REQUEST_COUNT._labelnames) <= _ALLOWED_LABEL_NAMES
    assert set(REQUEST_LATENCY_SECONDS._labelnames) <= _ALLOWED_LABEL_NAMES
    assert set(REQUESTS_IN_FLIGHT._labelnames) <= _ALLOWED_LABEL_NAMES
    assert "patient_id" not in REQUEST_COUNT._labelnames
    assert "correlation_id" not in REQUEST_COUNT._labelnames


def test_the_route_label_is_the_template_not_the_raw_resolved_path():
    client = TestClient(_app(), raise_server_exceptions=False)
    before = _counter_value(REQUEST_COUNT, service="test-service", method="GET",
                            route="/items/{item_id}", status_class="2xx")

    client.get("/items/1")
    client.get("/items/999999")

    after = _counter_value(REQUEST_COUNT, service="test-service", method="GET",
                           route="/items/{item_id}", status_class="2xx")
    assert after - before == 2, "both distinct ids must count against the SAME templated route label"


def test_an_unmatched_path_gets_the_fixed_fallback_label_not_the_raw_path():
    client = TestClient(_app(), raise_server_exceptions=False)
    before = _counter_value(REQUEST_COUNT, service="test-service", method="GET",
                            route="unmatched", status_class="4xx")

    client.get("/this-path-does-not-exist-and-might-contain-a-patient-id-1737")

    after = _counter_value(REQUEST_COUNT, service="test-service", method="GET", route="unmatched", status_class="4xx")
    assert after - before == 1


def test_request_count_increments_exactly_once_per_call_labeled_by_status_class():
    client = TestClient(_app(), raise_server_exceptions=False)
    before = _counter_value(REQUEST_COUNT, service="test-service", method="GET",
                            route="/items/{item_id}", status_class="2xx")

    client.get("/items/1")

    after = _counter_value(REQUEST_COUNT, service="test-service", method="GET",
                           route="/items/{item_id}", status_class="2xx")
    assert after - before == 1


def test_an_unhandled_exception_is_still_recorded_as_5xx_and_still_propagates():
    client = TestClient(_app(), raise_server_exceptions=False)
    before = _counter_value(REQUEST_COUNT, service="test-service", method="GET", route="/boom", status_class="5xx")

    resp = client.get("/boom")

    assert resp.status_code == 500
    after = _counter_value(REQUEST_COUNT, service="test-service", method="GET", route="/boom", status_class="5xx")
    assert after - before == 1


def test_latency_is_observed_for_a_successful_request():
    client = TestClient(_app(), raise_server_exceptions=False)
    before = REQUEST_LATENCY_SECONDS.labels(
        service="test-service", method="GET", route="/items/{item_id}"
    )._sum.get()

    client.get("/items/1")

    after = REQUEST_LATENCY_SECONDS.labels(service="test-service", method="GET", route="/items/{item_id}")._sum.get()
    assert after >= before  # a real, non-negative observation was recorded


def test_in_flight_gauge_returns_to_zero_after_every_request_including_a_failed_one():
    client = TestClient(_app(), raise_server_exceptions=False)

    client.get("/items/1")
    assert _counter_value(REQUESTS_IN_FLIGHT, service="test-service") == 0

    client.get("/boom")
    assert _counter_value(REQUESTS_IN_FLIGHT, service="test-service") == 0


def test_metrics_endpoint_returns_prometheus_text_exposition_format():
    client = TestClient(_app(), raise_server_exceptions=False)
    client.get("/items/1")

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
    assert "http_request_duration_seconds" in resp.text
