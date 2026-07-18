"""Endpoint-level tests for eligibility-service's Stage 3 additions
(app.py): the job-lifecycle HTTP surface and the visit-chat endpoint.

Uses FastAPI's TestClient against the real app, with the module-level Redis
client swapped for an in-memory fake — no live Redis, no live Bedrock (the
chat endpoint's real degrade path is exercised as-is, since this repo's own
default config has no Bedrock credential — see agent_wiring.py).
"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/eligibility-service/app.py", "eligibility_app")


class _FakeRedis:
    def __init__(self):
        self.strings = {}
        self.lists = {}
        self.sets = {}

    def get(self, key):
        return self.strings.get(key)

    def set(self, key, value, ex=None):
        self.strings[key] = value

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lpop(self, key):
        lst = self.lists.get(key)
        return lst.pop(0) if lst else None

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    def srem(self, key, value):
        self.sets.get(key, set()).discard(value)

    def smembers(self, key):
        return set(self.sets.get(key, set()))


@pytest.fixture
def client(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(app_mod, "_redis", lambda: fake_redis)
    # Never build a real Bedrock runtime or hit a real worker poll cadence.
    monkeypatch.setenv("ELIGIBILITY_AGENT_RUNTIME", "raw_bedrock")
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    with TestClient(app_mod.app) as c:
        yield c


# --- job lifecycle endpoints --------------------------------------------------


def test_create_job_returns_201_and_queued_status(client):
    resp = client.post("/eligibility/jobs", json={"insurance_id": "MEM1"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    assert "MEM1" not in str(body)  # insurance_id never echoed back in the job response


def test_create_job_rejects_blank_insurance_id(client):
    resp = client.post("/eligibility/jobs", json={"insurance_id": "  "})

    assert resp.status_code == 422


def test_repeated_create_with_same_idempotency_key_returns_the_same_job(client):
    first = client.post(
        "/eligibility/jobs", json={"insurance_id": "MEM1", "idempotency_key": "dup"}
    ).json()
    second = client.post(
        "/eligibility/jobs", json={"insurance_id": "MEM1", "idempotency_key": "dup"}
    ).json()

    assert first["job_id"] == second["job_id"]


def test_get_job_returns_current_status(client):
    created = client.post("/eligibility/jobs", json={"insurance_id": "MEM1"}).json()

    resp = client.get(f"/eligibility/jobs/{created['job_id']}")

    assert resp.status_code == 200
    assert resp.json()["job_id"] == created["job_id"]


def test_get_unknown_job_is_404(client):
    resp = client.get("/eligibility/jobs/does-not-exist")

    assert resp.status_code == 404


def test_retry_on_a_still_queued_job_is_409_not_500(client):
    created = client.post("/eligibility/jobs", json={"insurance_id": "MEM1"}).json()

    resp = client.post(f"/eligibility/jobs/{created['job_id']}/retry")

    assert resp.status_code == 409
    assert resp.json()["status"] == "queued"


def test_retry_on_unknown_job_is_404(client):
    resp = client.post("/eligibility/jobs/does-not-exist/retry")

    assert resp.status_code == 404


def test_create_job_enqueue_failure_is_a_503_not_an_unhandled_exception(monkeypatch):
    class _RaisingRedis:
        def get(self, key):
            raise ConnectionError("redis down")

        def set(self, key, value, ex=None):
            raise ConnectionError("redis down")

    monkeypatch.setattr(app_mod, "_redis", lambda: _RaisingRedis())
    with TestClient(app_mod.app) as client:
        resp = client.post("/eligibility/jobs", json={"insurance_id": "MEM1"})

    assert resp.status_code == 503


# --- visit-chat endpoint -------------------------------------------------------


def test_visit_message_degrades_safely_without_a_configured_bedrock_credential(client):
    # This repo's own default config has BEDROCK_MODEL_ID=changeme / unset —
    # live Bedrock is never available here by design (see agent_wiring.py).
    resp = client.post("/visits/visit-1/messages", json={"message": "am I covered?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["termination_reason"] == "provider_error"
    assert "manually" in body["reply"].lower()


def test_visit_message_rejects_an_empty_message(client):
    resp = client.post("/visits/visit-1/messages", json={"message": ""})

    assert resp.status_code == 422


def test_visit_message_never_echoes_patient_or_insurance_identifiers(client):
    resp = client.post(
        "/visits/visit-1/messages",
        json={"message": "check please", "patient_id": 42, "insurance_id": "SECRET-MEM-9"},
    )

    assert resp.status_code == 200
    assert "SECRET-MEM-9" not in resp.text
    assert "42" not in resp.text
