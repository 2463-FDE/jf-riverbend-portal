"""Each of the four Stage 6 in-scope services (gateway, records, scheduling,
ROI) actually exposes a scrapeable GET /metrics — and, for the three
service-to-service APIs, that endpoint carries the SAME internal-token
guard every other non-healthcheck route already does. Gateway's own
/metrics stays unauthenticated, matching its /healthz precedent (see
services/gateway/app.py::metrics for why).
"""
from fastapi.testclient import TestClient

from conftest import load_module

TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def test_records_service_exposes_metrics_and_requires_the_internal_token(monkeypatch):
    mod = load_module("services/records-service/app.py", "records_app_metrics")
    monkeypatch.setattr(mod.settings, "internal_service_token", TOKEN)
    client = TestClient(mod.app)

    denied = client.get("/metrics")
    assert denied.status_code == 401

    allowed = client.get("/metrics", headers={"X-Internal-Token": TOKEN})
    assert allowed.status_code == 200
    assert "http_requests_total" in allowed.text


def test_scheduling_service_exposes_metrics_and_requires_the_internal_token(monkeypatch):
    mod = load_module("services/scheduling-service/app.py", "scheduling_app_metrics")
    monkeypatch.setattr(mod.settings, "internal_service_token", TOKEN)
    client = TestClient(mod.app)

    denied = client.get("/metrics")
    assert denied.status_code == 401

    allowed = client.get("/metrics", headers={"X-Internal-Token": TOKEN})
    assert allowed.status_code == 200
    assert "http_requests_total" in allowed.text


def test_roi_service_exposes_metrics_and_requires_the_internal_token(monkeypatch):
    mod = load_module("services/roi-service/app.py", "roi_app_metrics")
    monkeypatch.setattr(mod.settings, "internal_service_token", TOKEN)
    client = TestClient(mod.app)

    denied = client.get("/metrics")
    assert denied.status_code == 401

    allowed = client.get("/metrics", headers={"X-Internal-Token": TOKEN})
    assert allowed.status_code == 200
    assert "http_requests_total" in allowed.text


def test_gateway_exposes_metrics_unauthenticated_like_healthz():
    mod = load_module("services/gateway/app.py", "gateway_app_metrics")
    client = TestClient(mod.app)

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert "http_requests_total" in resp.text
