"""services/records-service/app.py::_check_patient_grant_coverage.

PR #22 review round 6 (2026-08-08 — high): migration 014 ships
patient_access_grants empty, and SqlPatientAccessGate denies any patient with
no active grant, so a normal apply.sh + restart against a database that
already has patients could boot straight into a clinic-wide chart-access
outage. Round 5's version only logged a warning — nothing mechanically
stopped the bad deploy. This function now raises (refuses to start) when
ENVIRONMENT=production, and only warns otherwise (so make up/make seed against
the committed seed — 255 patients, 7 grants — keeps booting, per round 4). A
coverage-query FAILURE (missing table from an unapplied migration, DB
unreachable) is treated the same way as a coverage GAP: fatal in production,
a warning elsewhere (round 6 — a prior version swallowed this unconditionally,
letting production boot healthy exactly when coverage couldn't be verified).

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


def test_db_error_is_swallowed_outside_production(monkeypatch, caplog):
    # A coverage-check failure during ordinary dev/compose startup ordering
    # (DB not ready yet) must not turn `make up` into a crash loop.
    monkeypatch.setattr(app_mod.settings, "environment", "development")
    session = _FakeSession(fail=True)
    _patch_sessionmaker(monkeypatch, session)

    with caplog.at_level("WARNING"):
        app_mod._check_patient_grant_coverage()  # must not raise
    assert session.closed
    assert any("failed outside production" in r.message for r in caplog.records)


def test_db_error_is_fatal_in_production(monkeypatch):
    # Round 6 review: a prior version swallowed this unconditionally, so the
    # exact failure modes this guard exists to catch (migration 014 not
    # applied, DB unreachable) let production boot HEALTHY with coverage
    # never verified. Now treated the same as "the check ran and found a
    # gap": refuse to start.
    monkeypatch.setattr(app_mod.settings, "environment", "production")
    session = _FakeSession(fail=True)
    _patch_sessionmaker(monkeypatch, session)

    with pytest.raises(RuntimeError, match="coverage query failed") as exc_info:
        app_mod._check_patient_grant_coverage()
    assert session.closed
    # Never leak the raw exception text (a DB-driver error string can embed
    # the connection URL/password) — only the exception TYPE name.
    assert "SQLAlchemyError" in str(exc_info.value)
    assert "simulated connection drop" not in str(exc_info.value)
