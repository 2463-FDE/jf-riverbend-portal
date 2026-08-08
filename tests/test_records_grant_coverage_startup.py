"""services/records-service/app.py::_check_patient_grant_coverage.

PR #22 review round 6 (2026-08-08 — high): migration 014 ships
patient_access_grants empty, and SqlPatientAccessGate denies any patient with
no active grant, so a normal apply.sh + restart against a database that
already has patients could boot straight into a clinic-wide chart-access
outage. Round 5's version only logged a warning — nothing mechanically
stopped the bad deploy. This function now raises (refuses to start) when
ENVIRONMENT=production, and only warns otherwise (so make up/make seed against
the committed seed — 255 patients, 7 grants — keeps booting, per round 4).

Unit-level regression for that branching logic (fake DB session, no real
Postgres) — proves the mechanism the deploy-safety decision relies on. A
live-Postgres integration test that boots the real service against a
populated, zero-grant database with ENVIRONMENT=production and asserts the
process actually exits is the natural end-to-end follow-up (see
tests/integration/), not duplicated here.
"""
import pytest
from sqlalchemy.exc import SQLAlchemyError

from conftest import load_module

app_mod = load_module("services/records-service/app.py", "records_app_grant_coverage_startup")


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeSession:
    def __init__(self, unreachable_count=0, fail=False):
        self._unreachable_count = unreachable_count
        self._fail = fail
        self.closed = False

    def execute(self, _stmt):
        if self._fail:
            raise SQLAlchemyError("simulated connection drop")
        return _FakeScalarResult(self._unreachable_count)

    def close(self):
        self.closed = True


def _patch_sessionmaker(monkeypatch, session):
    # get_sessionmaker() normally returns a sessionmaker CALLABLE that
    # produces a session; the real call site is get_sessionmaker()().
    monkeypatch.setattr(app_mod, "get_sessionmaker", lambda: (lambda: session))


def test_production_refuses_to_start_when_patients_are_unreachable(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "environment", "production")
    session = _FakeSession(unreachable_count=3)
    _patch_sessionmaker(monkeypatch, session)

    with pytest.raises(RuntimeError, match="3 patient"):
        app_mod._check_patient_grant_coverage()
    assert session.closed  # the session is closed before the RuntimeError is raised


def test_production_does_not_raise_when_coverage_is_complete(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "environment", "production")
    session = _FakeSession(unreachable_count=0)
    _patch_sessionmaker(monkeypatch, session)

    app_mod._check_patient_grant_coverage()  # must not raise


def test_non_production_only_warns_never_raises(monkeypatch, caplog):
    # Covers the default ("development", this repo's own .env) and the
    # committed seed's actual shape (255 patients, 7 grants) without needing
    # a real database — make up/make seed must keep booting (round 4).
    monkeypatch.setattr(app_mod.settings, "environment", "development")
    session = _FakeSession(unreachable_count=5)
    _patch_sessionmaker(monkeypatch, session)

    with caplog.at_level("WARNING"):
        app_mod._check_patient_grant_coverage()  # must not raise
    assert any("5 patient" in r.message for r in caplog.records)


def test_db_error_is_swallowed_in_every_environment(monkeypatch):
    for env in ("production", "development"):
        monkeypatch.setattr(app_mod.settings, "environment", env)
        session = _FakeSession(fail=True)
        _patch_sessionmaker(monkeypatch, session)
        # A coverage-check failure (e.g. DB unavailable) must never itself
        # become a reason the service won't start, in ANY environment.
        app_mod._check_patient_grant_coverage()
        assert session.closed
