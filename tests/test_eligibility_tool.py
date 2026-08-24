"""Unit tests for the two eligibility tools
(libs/eligibility_agent/eligibility_tool.py) — the only tools exposed to
either AgentRuntime. All calls go through httpx.MockTransport; no network, no
live eligibility-service.
"""
import logging
from datetime import datetime, timezone

import httpx

from libs.eligibility_agent.contracts import EligibilityStatus, VisitContext
from libs.eligibility_agent.eligibility_tool import EligibilityToolConfig, GetCoverageOnFileTool, VerifyCurrentEligibilityTool


def _context(insurance_id=None, **coverage_fields):
    return VisitContext(
        visit_id="visit-1", insurance_id=insurance_id, updated_at=datetime.now(timezone.utc), **coverage_fields
    )


def _transport(status_json):
    def handler(request):
        return httpx.Response(200, json=status_json)

    return httpx.MockTransport(handler)


def _verify_tool(context, transport, **config_overrides):
    # payer_configured defaults True here, not because that reflects this
    # repo's real (permanently unconfigured) state, but because these tests
    # are about the live-call MECHANICS (response parsing, error handling,
    # PHI-safe logging, internal-token forwarding) — the simulated/unavailable
    # split below is what actually exercises the payer_configured gate.
    config_overrides.setdefault("payer_configured", True)
    return VerifyCurrentEligibilityTool(context, transport=transport, config=EligibilityToolConfig(**config_overrides))


# --- VerifyCurrentEligibilityTool: live-check mechanics (payer_configured=True) --


def test_no_insurance_on_file_returns_unavailable_without_a_network_call():
    def handler(request):
        raise AssertionError("must not call eligibility-service with no insurance on file")

    tool = _verify_tool(_context(insurance_id=None), httpx.MockTransport(handler))

    result = tool.invoke({})

    assert result.ok is True
    assert result.payload["outcome"] == "unavailable"
    assert result.payload["status"] == EligibilityStatus.UNKNOWN.value
    assert "no insurance member id" in result.payload["note"].lower()


def test_active_status_passed_through_as_verified():
    tool = _verify_tool(
        _context("BCBS1"), _transport({"status": "active", "checked_at": "2026-07-17T12:00:00Z"})
    )

    result = tool.invoke({})

    assert result.payload["outcome"] == "verified"
    assert result.payload["status"] == "active"
    assert result.payload["as_of"] == "2026-07-17T12:00:00Z"


def test_inactive_status_passed_through_as_verified():
    tool = _verify_tool(_context("BCBS1"), _transport({"status": "inactive"}))

    result = tool.invoke({})

    assert result.payload["outcome"] == "verified"
    assert result.payload["status"] == "inactive"


def test_pending_status_note_says_in_progress():
    tool = _verify_tool(_context("BCBS1"), _transport({"status": "pending"}))

    result = tool.invoke({})

    assert result.payload["outcome"] == "verified"
    assert result.payload["status"] == "pending"
    assert "in progress" in result.payload["note"].lower()


def test_a_degraded_response_with_an_error_type_is_unavailable_not_verified():
    # eligibility-service's own EligibilityResponse.error is set whenever
    # check.py fell back to cache-or-unknown — that is never a genuine new
    # verification, regardless of what status it carries.
    tool = _verify_tool(
        _context("BCBS1", coverage_status="active"),
        _transport({"status": "stale", "error": "CircuitOpenError"}),
    )

    result = tool.invoke({})

    assert result.payload["outcome"] == "unavailable"
    assert result.payload["status"] == "active"  # the STORED status, not the degraded "stale"
    assert "stored record remains active" in result.payload["note"].lower()


def test_http_error_status_with_a_json_body_is_unavailable_not_verified():
    # w-9-2-planner P1a review fix (B3-http-errors-verified): a 500/403
    # response whose JSON body happens to lack an "error" key used to be
    # parsed as a normal success — outcome="verified" with a defaulted
    # status="unknown" that then got PERSISTED, overwriting a known-good
    # stored status. The status code must be checked BEFORE any parsing.
    def handler(request):
        return httpx.Response(500, json={"detail": "internal server error"})

    tool = _verify_tool(_context("BCBS1", coverage_status="active"), httpx.MockTransport(handler))

    result = tool.invoke({})

    assert result.payload["outcome"] == "unavailable"
    assert result.payload["status"] == "active"  # the STORED status is preserved, not overwritten


def test_a_2xx_response_missing_the_status_field_is_unavailable_not_verified():
    # Defense in depth: EligibilityResponse.status is a required field, so a
    # 2xx body missing it means an unexpected shape, not a genuine "unknown"
    # check result — must not be silently defaulted and marked "verified".
    def handler(request):
        return httpx.Response(200, json={"checked_at": "2026-07-17T12:00:00Z"})

    tool = _verify_tool(_context("BCBS1", coverage_status="active"), httpx.MockTransport(handler))

    result = tool.invoke({})

    assert result.payload["outcome"] == "unavailable"
    assert result.payload["status"] == "active"


def test_extra_argument_rejected_before_any_network_call():
    def handler(request):
        raise AssertionError("must not call eligibility-service with malformed arguments")

    tool = _verify_tool(_context("BCBS1"), httpx.MockTransport(handler))

    result = tool.invoke({"insurance_id": "smuggled"})

    assert result.ok is False
    assert result.payload["error"] == "invalid_arguments"


def test_transport_failure_degrades_to_unavailable_without_leaking_the_exception():
    secret = "member-secret-123"

    def handler(request):
        raise httpx.ConnectError(f"could not reach payer for {secret}", request=request)

    tool = _verify_tool(_context("BCBS1"), httpx.MockTransport(handler))

    result = tool.invoke({})

    assert result.ok is True
    assert result.payload["outcome"] == "unavailable"


def test_transport_failure_logs_error_type_only_never_the_exception_text(caplog):
    secret = "member-secret-123"

    def handler(request):
        raise httpx.ConnectError(f"could not reach payer for {secret}", request=request)

    tool = _verify_tool(_context("BCBS1"), httpx.MockTransport(handler))

    with caplog.at_level(logging.WARNING):
        result = tool.invoke({})

    assert result.payload["outcome"] == "unavailable"
    assert caplog.records, "expected the degraded path to log something for operational triage"
    for record in caplog.records:
        message = record.getMessage()
        assert secret not in message
        assert "ConnectError" in message


# --- VerifyCurrentEligibilityTool: the payer_configured gate (w-9-2-planner P1a) --


def test_unconfigured_payer_returns_simulated_without_any_network_call():
    def handler(request):
        raise AssertionError("must not call eligibility-service when no payer is configured")

    tool = VerifyCurrentEligibilityTool(
        _context("BCBS1", coverage_status="unknown"),
        transport=httpx.MockTransport(handler),
        config=EligibilityToolConfig(payer_configured=False),
    )

    result = tool.invoke({})

    assert result.payload["outcome"] == "simulated"
    assert "no real payer contacted" in result.payload["note"].lower()


def test_unconfigured_payer_never_invents_active_it_echoes_the_stored_status():
    tool = VerifyCurrentEligibilityTool(
        _context("BCBS1", coverage_status="active"),
        transport=None,
        config=EligibilityToolConfig(payer_configured=False),
    )

    result = tool.invoke({})

    assert result.payload["outcome"] == "simulated"
    assert result.payload["status"] == "active"  # echoes stored, never invents


def test_default_payer_configured_reads_from_the_environment(monkeypatch):
    monkeypatch.delenv("PAYER_API_KEY", raising=False)
    assert EligibilityToolConfig().payer_configured is False

    monkeypatch.setenv("PAYER_API_KEY", "a-real-key")
    assert EligibilityToolConfig().payer_configured is True


# --- branch 7: this tool is one of eligibility-service's callers -------------


def test_the_tool_sends_the_internal_service_token(monkeypatch):
    """Adversarial review of #43. eligibility-service verifies its callers now,
    and this tool calls it directly — it is neither a gateway proxy nor the
    intake enqueue, so it was missed when those two were covered.

    The failure it would have caused is the quiet kind: a 401 is swallowed by
    the tool's own except, and the assistant reports UNKNOWN coverage. A wrong
    answer that looks like a legitimate one, rather than an error anybody
    would notice.
    """
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "t" * 32)
    captured = {}

    def handler(request):
        captured["token"] = request.headers.get("X-Internal-Token")
        return httpx.Response(200, json={"status": "active", "checked_at": None})

    tool = _verify_tool(_context(insurance_id="M-1"), httpx.MockTransport(handler))
    tool.invoke({})

    assert captured["token"] == "t" * 32


def test_the_token_can_be_passed_explicitly_rather_than_read_from_the_env():
    """So a caller that manages its own configuration is not forced through a
    process-wide environment variable."""
    captured = {}

    def handler(request):
        captured["token"] = request.headers.get("X-Internal-Token")
        return httpx.Response(200, json={"status": "active", "checked_at": None})

    tool = _verify_tool(
        _context(insurance_id="M-1"),
        httpx.MockTransport(handler),
        internal_service_token="explicit-token-value",
    )
    tool.invoke({})

    assert captured["token"] == "explicit-token-value"


# --- GetCoverageOnFileTool: pure read, no network call ever -----------------


def test_no_coverage_on_file_is_reported_plainly():
    tool = GetCoverageOnFileTool(_context())

    result = tool.invoke({})

    assert result.ok is True
    assert result.payload["has_coverage_on_file"] is False
    assert "no coverage on file" in result.payload["note"].lower()


def test_stored_coverage_is_returned_verbatim_with_no_network_call():
    ctx = _context(
        coverage_payer_name="Kaiser",
        coverage_plan_type="HMO",
        coverage_member_id_masked="******5591",
        coverage_status="active",
        coverage_verified_at=datetime(2026, 3, 5, 8, 55, 0, tzinfo=timezone.utc),
    )
    tool = GetCoverageOnFileTool(ctx)

    result = tool.invoke({})

    assert result.payload["has_coverage_on_file"] is True
    assert result.payload["payer_name"] == "Kaiser"
    assert result.payload["plan_type"] == "HMO"
    assert result.payload["member_id_masked"] == "******5591"
    assert result.payload["status"] == "active"
    assert result.payload["verified_at"] == "2026-03-05T08:55:00+00:00"


def test_never_shows_a_full_member_id():
    ctx = _context(coverage_payer_name="Kaiser", coverage_member_id_masked="******5591", coverage_status="active")
    tool = GetCoverageOnFileTool(ctx)

    result = tool.invoke({})

    assert "5591" in result.payload["member_id_masked"]
    assert result.payload["member_id_masked"].count("*") > 0


def test_extra_argument_rejected():
    tool = GetCoverageOnFileTool(_context(coverage_payer_name="Kaiser", coverage_status="active"))

    result = tool.invoke({"patient_id": 9999})

    assert result.ok is False
    assert result.payload["error"] == "invalid_arguments"
