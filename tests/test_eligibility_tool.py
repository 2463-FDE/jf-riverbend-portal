"""Unit tests for the check_eligibility tool
(libs/eligibility_agent/eligibility_tool.py) — the only tool exposed to
either AgentRuntime, and the sole path from an agent into Stage 1's resilient
eligibility service. All calls go through httpx.MockTransport; no network, no
live eligibility-service.
"""
import logging
from datetime import datetime, timezone

import httpx

from libs.eligibility_agent.contracts import EligibilityStatus, VisitContext
from libs.eligibility_agent.eligibility_tool import CheckEligibilityTool


def _context(insurance_id=None):
    return VisitContext(visit_id="visit-1", insurance_id=insurance_id, updated_at=datetime.now(timezone.utc))


def _transport(status_json):
    def handler(request):
        return httpx.Response(200, json=status_json)

    return httpx.MockTransport(handler)


def test_no_insurance_on_file_returns_unknown_without_a_network_call():
    def handler(request):
        raise AssertionError("must not call eligibility-service with no insurance on file")

    tool = CheckEligibilityTool(_context(insurance_id=None), transport=httpx.MockTransport(handler))

    result = tool.invoke({})

    assert result.ok is True
    assert result.payload["status"] == EligibilityStatus.UNKNOWN.value
    assert "no insurance on file" in result.payload["note"].lower()


def test_active_status_passed_through():
    tool = CheckEligibilityTool(
        _context("BCBS1"), transport=_transport({"status": "active", "checked_at": "2026-07-17T12:00:00Z"})
    )

    result = tool.invoke({})

    assert result.payload["status"] == "active"
    assert result.payload["as_of"] == "2026-07-17T12:00:00Z"


def test_inactive_status_passed_through():
    tool = CheckEligibilityTool(_context("BCBS1"), transport=_transport({"status": "inactive"}))

    result = tool.invoke({})

    assert result.payload["status"] == "inactive"


def test_pending_status_note_says_in_progress():
    tool = CheckEligibilityTool(_context("BCBS1"), transport=_transport({"status": "pending"}))

    result = tool.invoke({})

    assert result.payload["status"] == "pending"
    assert "in progress" in result.payload["note"].lower()


def test_stale_status_note_warns_it_may_be_outdated():
    tool = CheckEligibilityTool(_context("BCBS1"), transport=_transport({"status": "stale"}))

    result = tool.invoke({})

    assert result.payload["status"] == "stale"
    assert "outdated" in result.payload["note"].lower()


def test_extra_argument_rejected_before_any_network_call():
    def handler(request):
        raise AssertionError("must not call eligibility-service with malformed arguments")

    tool = CheckEligibilityTool(_context("BCBS1"), transport=httpx.MockTransport(handler))

    result = tool.invoke({"insurance_id": "smuggled"})

    assert result.ok is False
    assert result.payload["error"] == "invalid_arguments"


def test_transport_failure_degrades_to_unknown_without_leaking_the_exception():
    secret = "member-secret-123"

    def handler(request):
        raise httpx.ConnectError(f"could not reach payer for {secret}", request=request)

    tool = CheckEligibilityTool(_context("BCBS1"), transport=httpx.MockTransport(handler))

    result = tool.invoke({})

    assert result.ok is True
    assert result.payload["status"] == EligibilityStatus.UNKNOWN.value


def test_transport_failure_logs_error_type_only_never_the_exception_text(caplog):
    secret = "member-secret-123"

    def handler(request):
        raise httpx.ConnectError(f"could not reach payer for {secret}", request=request)

    tool = CheckEligibilityTool(_context("BCBS1"), transport=httpx.MockTransport(handler))

    with caplog.at_level(logging.WARNING):
        result = tool.invoke({})

    assert result.payload["status"] == EligibilityStatus.UNKNOWN.value
    assert caplog.records, "expected the degraded path to log something for operational triage"
    for record in caplog.records:
        message = record.getMessage()
        assert secret not in message
        assert "ConnectError" in message
