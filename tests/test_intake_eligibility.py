"""Unit tests for intake-service's Stage 3 async eligibility enqueue
(app.py::_start_eligibility_check).

Stage 3 replaces the old inline, unbounded payer call (RIV-088/RIV-141) with
one bounded HTTP call that only asks eligibility-service to enqueue a job —
never the payer round-trip itself. A failure to even enqueue must still map
to "unknown", never "inactive", and must never leak a raw exception message
(which can echo the request URL, including the member_id).
"""
import httpx
import pytest

from conftest import load_module

app_mod = load_module("services/intake-service/app.py", "intake_app")

Insurance = app_mod.Insurance
_start_eligibility_check = app_mod._start_eligibility_check


def test_no_insurance_returns_all_none():
    eligibility, status, job_id = _start_eligibility_check(None, patient_id=1, coverage_id=None, correlation_id="c1")

    assert eligibility is None
    assert status is None
    assert job_id is None


def test_no_member_id_returns_all_none():
    ins = Insurance(payer_name="Aetna")

    eligibility, status, job_id = _start_eligibility_check(ins, patient_id=1, coverage_id=None, correlation_id="c1")

    assert eligibility is None
    assert status is None
    assert job_id is None


def test_happy_path_enqueues_and_returns_pending(monkeypatch):
    ins = Insurance(payer_name="Aetna", member_id="MEM1")
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"job_id": "abc123", "status": "queued"}

    def _fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(app_mod.httpx, "post", _fake_post)

    eligibility, status, job_id = _start_eligibility_check(
        ins, patient_id=7, coverage_id=42, correlation_id="corr-1"
    )

    assert status == "pending"
    assert job_id == "abc123"
    assert eligibility["status"] == "pending"
    assert eligibility["insurance_id"] == "MEM1"
    assert captured["url"].endswith("/eligibility/jobs")
    assert captured["json"]["insurance_id"] == "MEM1"
    assert captured["json"]["idempotency_key"] == "patient:7:coverage:42"
    assert captured["headers"]["X-Request-Id"] == "corr-1"
    # Bounded — never the unbounded old inline call.
    assert captured["timeout"] == app_mod.settings.eligibility_job_enqueue_timeout_seconds


def test_enqueue_call_is_bounded_never_the_old_unbounded_style(monkeypatch):
    # Regression guard for RIV-088/RIV-141: the new call must always pass an
    # explicit timeout, never rely on httpx's default (which is not "no
    # timeout" for the underlying old bug, but this asserts the new code path
    # never regresses to an unbounded call either).
    calls = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"job_id": "j1", "status": "queued"}

    def _fake_post(url, *, json, headers, timeout):
        calls["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(app_mod.httpx, "post", _fake_post)

    _start_eligibility_check(
        Insurance(payer_name="Aetna", member_id="MEM1"), patient_id=1, coverage_id=1, correlation_id="c1"
    )

    assert calls["timeout"] is not None
    assert calls["timeout"] <= 10  # sanity bound, nowhere near "spin ~4-5s" let alone RIV-141's ~20 min


def test_transport_failure_maps_to_unknown_not_inactive(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("connection refused to host with member_id=MEM1-SECRET")

    monkeypatch.setattr(app_mod.httpx, "post", _raise)

    eligibility, status, job_id = _start_eligibility_check(
        Insurance(payer_name="Aetna", member_id="MEM1-SECRET"), patient_id=1, coverage_id=1, correlation_id="c1"
    )

    assert status == "unknown"
    assert status != "inactive"
    assert job_id is None
    assert eligibility["active"] is False


def test_transport_failure_error_field_is_exception_type_only(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("connection refused to host with member_id=MEM1-SECRET")

    monkeypatch.setattr(app_mod.httpx, "post", _raise)

    eligibility, _, _ = _start_eligibility_check(
        Insurance(payer_name="Aetna", member_id="MEM1-SECRET"), patient_id=1, coverage_id=1, correlation_id="c1"
    )

    assert eligibility["error"] == "ConnectError"
    assert "MEM1-SECRET" not in eligibility["error"]


def test_transport_failure_does_not_log_raw_exception_message(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.ERROR, logger=app_mod.log.name)

    def _raise(*a, **k):
        raise httpx.ConnectError("connection refused to host with member_id=MEM1-SECRET")

    monkeypatch.setattr(app_mod.httpx, "post", _raise)

    _start_eligibility_check(
        Insurance(payer_name="Aetna", member_id="MEM1-SECRET"), patient_id=1, coverage_id=1, correlation_id="c1"
    )

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "MEM1-SECRET" not in log_text
    assert "ConnectError" in log_text


def test_http_error_status_from_eligibility_service_maps_to_unknown(monkeypatch):
    def _fake_post(url, *, json, headers, timeout):
        request = httpx.Request("POST", url)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("service unavailable", request=request, response=response)

    monkeypatch.setattr(app_mod.httpx, "post", _fake_post)

    eligibility, status, job_id = _start_eligibility_check(
        Insurance(payer_name="Aetna", member_id="MEM1"), patient_id=1, coverage_id=1, correlation_id="c1"
    )

    assert status == "unknown"
    assert job_id is None


def test_transport_failure_fallback_has_the_shared_contract_shape(monkeypatch):
    monkeypatch.setattr(
        app_mod.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.TimeoutException("t"))
    )

    eligibility, _, _ = _start_eligibility_check(
        Insurance(payer_name="Aetna", member_id="MEM1"), patient_id=1, coverage_id=1, correlation_id="c1"
    )

    assert set(eligibility.keys()) == {
        "insurance_id",
        "active",
        "status",
        "payer",
        "raw_status",
        "checked_at",
        "stale",
        "error",
    }
    assert eligibility["stale"] is False
    assert eligibility["insurance_id"] == "MEM1"
    assert eligibility["payer"] == "Aetna"
