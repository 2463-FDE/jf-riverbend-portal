"""Codex review (2026-08-07, PR #23) — services/records-service/app.py::
list_patients (GET /patients) had NO internal-token check even after every
other route in this file was gated. records-service's port is published to
the host (docker-compose.yml), so a direct caller could skip the gateway's
require_session check entirely and enumerate every patient's id/name/DOB/
MRN. This route stays deliberately staff-broad (not patient-scoped — see
the route's own docstring for why), but that design decision doesn't excuse
it from proving the call came through the gateway at all.
"""
import types

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_list_patients")

TEST_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


class _FakeScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return len(self._rows)


class FakeSession:
    def __init__(self, *, fail=False):
        self._fail = fail
        self._rows = [
            types.SimpleNamespace(id=1042, mrn="M4471", name="Maria Gonzalez", dob="1971-03-02", gender="F", created_at=None)
        ]

    def execute(self, _stmt):
        if self._fail:
            raise SQLAlchemyError("simulated db failure")
        return _FakeScalarsResult(self._rows)


def _fake_get_db():
    yield FakeSession()


def _fake_get_db_failing():
    yield FakeSession(fail=True)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", TEST_TOKEN)
    app_mod.app.dependency_overrides[app_mod.get_db] = _fake_get_db
    yield TestClient(app_mod.app)
    app_mod.app.dependency_overrides.clear()


def test_direct_caller_without_internal_token_is_rejected(client):
    # The bypass this fix closes: records-service's host port is published,
    # so a caller could hit this route directly, skipping the gateway's own
    # require_session check entirely.
    resp = client.get("/patients")
    assert resp.status_code == 401


def test_wrong_internal_token_is_rejected(client):
    resp = client.get("/patients", headers={"X-Internal-Token": "not-the-real-token"})
    assert resp.status_code == 401


def test_valid_internal_token_returns_the_roster(client):
    resp = client.get("/patients", headers={"X-Internal-Token": TEST_TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == 1042


def test_this_route_does_not_require_an_actor_identity():
    # Deliberate design decision (see the route's docstring): staff-broad,
    # not patient-scoped, so no X-Actor-Id is needed once the internal
    # token proves the call came through the gateway.
    import inspect

    source = inspect.getsource(app_mod.list_patients)
    assert "x_actor_id" not in source
