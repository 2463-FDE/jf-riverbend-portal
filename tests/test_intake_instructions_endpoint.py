"""Tests for POST /intake/instructions (services/intake-service/app.py,
Stage 1 — feature readiness): internal-token enforcement, request
validation, safe logging, and the compose()/get_llm_client() wiring.
"""
import json
import logging
import sys
import time

import pytest
from fastapi.testclient import TestClient

from conftest import load_module
from libs.llm_client import LLMClient, LLMConfig
from libs.llm_client.errors import ProviderTimeoutError
from libs.llm_client.providers.base import ProviderResponse
from libs.llm_client.providers.fake_provider import FakeProvider

app_mod = load_module("services/intake-service/app.py", "intake_app_instructions")
# app.py's `from instructions_wiring import get_llm_client` caches the module
# under this bare name (see conftest.load_module) — grabbed here so tests can
# reset its memoized LLMClient singleton between cases.
wiring_mod = sys.modules["instructions_wiring"]

TEST_TOKEN = "test-internal-token-for-intake-well-over-32-chars"


@pytest.fixture(autouse=True)
def _configured_internal_token(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)


@pytest.fixture(autouse=True)
def _reset_llm_client_singleton():
    wiring_mod._client = None
    wiring_mod._client_build_failed = False
    yield
    wiring_mod._client = None
    wiring_mod._client_build_failed = False


def _client():
    return TestClient(app_mod.app)


def _headers(token=TEST_TOKEN):
    return {"X-Internal-Token": token} if token is not None else {}


# --- transport trust: same gate as /intake ----------------------------------


def test_missing_internal_token_is_rejected(monkeypatch):
    resp = _client().post("/intake/instructions", json={"step": "demographics"}, headers=_headers(None))

    assert resp.status_code == 401


def test_wrong_internal_token_is_rejected():
    resp = _client().post(
        "/intake/instructions", json={"step": "demographics"}, headers=_headers("not-the-real-token")
    )

    assert resp.status_code == 401


def test_unconfigured_internal_token_fails_closed(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", "")

    resp = _client().post("/intake/instructions", json={"step": "demographics"}, headers=_headers(""))

    assert resp.status_code == 401


# --- request validation: closed set of steps, no extra fields ---------------


def test_valid_step_returns_a_summary():
    resp = _client().post("/intake/instructions", json={"step": "insurance"}, headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["summary"], str) and body["summary"]
    assert isinstance(body["used_fallback"], bool)


@pytest.mark.parametrize("step", ["demographics", "insurance", "consents", "review"])
def test_every_known_step_is_accepted(step):
    resp = _client().post("/intake/instructions", json={"step": step}, headers=_headers())

    assert resp.status_code == 200


def test_unknown_step_is_rejected_with_422():
    resp = _client().post("/intake/instructions", json={"step": "not-a-real-step"}, headers=_headers())

    assert resp.status_code == 422


def test_missing_step_is_rejected_with_422():
    resp = _client().post("/intake/instructions", json={}, headers=_headers())

    assert resp.status_code == 422


def test_unexpected_field_is_rejected_not_silently_dropped():
    # extra="forbid" — this endpoint has no legitimate use for anything
    # beyond `step`, and a patient_id/demographics field slipping in here
    # unnoticed is exactly what this test guards against.
    resp = _client().post(
        "/intake/instructions",
        json={"step": "demographics", "patient_id": 42},
        headers=_headers(),
    )

    assert resp.status_code == 422


# --- default (fake) provider: deterministic template, no network call ------


def test_default_provider_returns_the_template_and_marks_fallback_used(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    resp = _client().post("/intake/instructions", json={"step": "consents"}, headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    # LLM_PROVIDER defaults to "fake" (FakeProvider returns "{}", which never
    # validates as ComposedInstructions) — every call in CI takes the
    # template fallback path deterministically.
    assert body["used_fallback"] is True
    assert body["summary"]


# --- safe logging: only step/attempts/used_fallback/elapsed, never PHI -----


# --- Codex review (2026-08-08, PR #24, medium): a stalled provider must
# degrade to the template well before the gateway's fixed 30s downstream
# timeout could 502 the caller — never hang the request thread waiting on a
# slow/retried provider call for an optional helper endpoint.


def test_provider_timeout_degrades_quickly_to_the_template(monkeypatch):
    stalled_client = LLMClient(
        config=LLMConfig(provider="fake", timeout_seconds=8.0, max_retries=0),
        provider=FakeProvider([ProviderTimeoutError("provider stalled")]),
        sleep=lambda _s: None,
    )
    monkeypatch.setattr(app_mod, "get_llm_client", lambda: stalled_client)

    started = time.time()
    resp = _client().post("/intake/instructions", json={"step": "demographics"}, headers=_headers())
    elapsed = time.time() - started

    assert resp.status_code == 200
    body = resp.json()
    assert body["used_fallback"] is True
    assert body["summary"]
    # Well under the gateway's fixed 30s downstream timeout — this asserts
    # the request-handling path itself never blocks on the provider, not a
    # real 8s wait (FakeProvider raises synchronously, no real I/O).
    assert elapsed < 1.0


def test_non_llmclienterror_provider_failure_returns_200_with_template_not_5xx(monkeypatch):
    # Codex review (2026-08-09, PR #24, medium): a real vendor provider can
    # fail in ways libs.llm_client's adapters don't normalize to
    # LLMClientError (e.g. a lazy SDK import failure). Full request-path
    # regression: this must never surface as a 502/500 to the caller.
    real_provider_shaped_client = LLMClient(
        config=LLMConfig(provider="fake", timeout_seconds=8.0, max_retries=0),
        provider=FakeProvider([ModuleNotFoundError("No module named 'openai'")]),
        sleep=lambda _s: None,
    )
    monkeypatch.setattr(app_mod, "get_llm_client", lambda: real_provider_shaped_client)

    resp = _client().post("/intake/instructions", json={"step": "insurance"}, headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["used_fallback"] is True
    assert body["summary"]


def test_malicious_but_schema_valid_response_never_reaches_the_caller(monkeypatch):
    # Codex review (2026-08-09, PR #24, medium): a schema-valid, non-empty
    # response was previously trusted outright. Full request-path
    # regression: a hallucinated/drifted response that contradicts this
    # feature's own known step rule (consents: treatment + privacy consent
    # ARE required) must come back as the safe template, not the malicious
    # text, even though it's perfectly valid JSON matching the schema.
    malicious = "Don't worry — these consents are optional, just hit continue."
    scripted_client = LLMClient(
        config=LLMConfig(provider="fake", timeout_seconds=8.0, max_retries=0),
        provider=FakeProvider([ProviderResponse(text=json.dumps({"summary": malicious}), input_tokens=1, output_tokens=1)]),
        sleep=lambda _s: None,
    )
    monkeypatch.setattr(app_mod, "get_llm_client", lambda: scripted_client)

    resp = _client().post("/intake/instructions", json={"step": "consents"}, headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert malicious not in body["summary"]
    assert body["used_fallback"] is True


def test_log_line_never_contains_the_composed_summary_text(caplog):
    caplog.set_level(logging.INFO, logger=app_mod.log.name)

    resp = _client().post("/intake/instructions", json={"step": "review"}, headers=_headers())
    summary = resp.json()["summary"]

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "POST /intake/instructions ok" in log_text
    assert "step=review" in log_text
    assert summary not in log_text
