"""Unit tests for intake-service's eligibility fallback (app.py::_verify_eligibility).

Stage 1 bug fix: a failure to REACH eligibility-service (a transport failure)
must map to status "unknown", never to "active": False on its own, and must
never leak a raw exception message (which can echo the request URL,
including the member_id) into a log line or the returned dict.
"""
import httpx
import pytest

from conftest import load_module

app_mod = load_module("services/intake-service/app.py", "intake_app")

Insurance = app_mod.Insurance
_verify_eligibility = app_mod._verify_eligibility


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # The 4.2s artificial delay (RIV-088) is out of Stage 1's scope to remove,
    # but tests shouldn't have to wait for it.
    monkeypatch.setattr(app_mod.time, "sleep", lambda seconds: None)


def test_no_insurance_returns_none():
    assert _verify_eligibility(None) is None


def test_no_member_id_returns_none():
    assert _verify_eligibility(Insurance(payer_name="Aetna")) is None


def test_happy_path_passes_through_eligibility_service_response(monkeypatch):
    upstream = {"insurance_id": "MEM1", "active": True, "status": "active", "checked_at": "2026-07-17T12:00:00Z"}

    class _FakeResponse:
        def json(self):
            return upstream

    monkeypatch.setattr(app_mod.httpx, "get", lambda *a, **k: _FakeResponse())

    result = _verify_eligibility(Insurance(payer_name="Aetna", member_id="MEM1"))

    assert result == upstream


def test_transport_failure_maps_to_unknown_not_inactive(monkeypatch, caplog):
    def _raise(*a, **k):
        raise httpx.ConnectError(f"connection refused to host with member_id=MEM1-SECRET")

    monkeypatch.setattr(app_mod.httpx, "get", _raise)

    result = _verify_eligibility(Insurance(payer_name="Aetna", member_id="MEM1-SECRET"))

    assert result["status"] == "unknown"
    assert result["status"] != "inactive"
    assert result["active"] is False


def test_transport_failure_error_field_is_exception_type_only(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("connection refused to host with member_id=MEM1-SECRET")

    monkeypatch.setattr(app_mod.httpx, "get", _raise)

    result = _verify_eligibility(Insurance(payer_name="Aetna", member_id="MEM1-SECRET"))

    assert result["error"] == "ConnectError"
    assert "MEM1-SECRET" not in result["error"]


def test_transport_failure_does_not_log_raw_exception_message(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.ERROR, logger=app_mod.log.name)

    def _raise(*a, **k):
        raise httpx.ConnectError("connection refused to host with member_id=MEM1-SECRET")

    monkeypatch.setattr(app_mod.httpx, "get", _raise)

    _verify_eligibility(Insurance(payer_name="Aetna", member_id="MEM1-SECRET"))

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "MEM1-SECRET" not in log_text
    assert "ConnectError" in log_text


def test_transport_failure_fallback_has_the_shared_contract_shape(monkeypatch):
    monkeypatch.setattr(app_mod.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.TimeoutException("t")))

    result = _verify_eligibility(Insurance(payer_name="Aetna", member_id="MEM1"))

    assert set(result.keys()) == {
        "insurance_id",
        "active",
        "status",
        "payer",
        "raw_status",
        "checked_at",
        "stale",
        "error",
    }
    assert result["stale"] is False
    assert result["insurance_id"] == "MEM1"
    assert result["payer"] == "Aetna"
