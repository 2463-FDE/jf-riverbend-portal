"""services/gateway/production_guard.py — W10 Final Stage 1, sub-slice 3.

Unit tests against the guard function itself (a real in-memory SQLite
session, no FastAPI/TestClient needed) — see test_gateway_startup_roles_config
-style tests in test_gateway_boot_guard.py for the lifespan wiring itself.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_production_guard")
production_guard = app_mod.production_guard


class _Settings:
    def __init__(self, *, payer_integration_mode="live", payer_api_key="a-real-key"):
        self.payer_integration_mode = payer_integration_mode
        self.payer_api_key = payer_api_key


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(app_mod.mfa_config, "effective_mode", lambda: "enforce")
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    app_mod.User.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        yield s


def _seed(db, **kwargs):
    defaults = dict(
        username="realclinician", password_hash="pbkdf2_sha256$260000$a-real-salt$abc",
        role="clinician", is_active=True, mfa_shared_account=False,
    )
    defaults.update(kwargs)
    db.add(app_mod.User(**defaults))
    db.commit()


def test_a_fully_migrated_safe_posture_reports_no_problems(db, monkeypatch):
    monkeypatch.setattr(production_guard.os, "getenv", lambda k, default="": "us.anthropic.claude-x" if k == "BEDROCK_MODEL_ID" else default)
    _seed(db)

    assert production_guard.check(db, _Settings()) == []


def test_mfa_off_is_a_problem(db, monkeypatch):
    monkeypatch.setattr(app_mod.mfa_config, "effective_mode", lambda: "off")
    monkeypatch.setattr(production_guard.os, "getenv", lambda k, default="": "us.anthropic.claude-x" if k == "BEDROCK_MODEL_ID" else default)
    _seed(db)

    problems = production_guard.check(db, _Settings())
    assert any("MFA" in p for p in problems)


def test_simulation_payer_mode_is_a_problem(db, monkeypatch):
    monkeypatch.setattr(production_guard.os, "getenv", lambda k, default="": "us.anthropic.claude-x" if k == "BEDROCK_MODEL_ID" else default)
    _seed(db)

    problems = production_guard.check(db, _Settings(payer_integration_mode="simulation"))
    assert any("PAYER_INTEGRATION_MODE" in p for p in problems)


def test_placeholder_model_id_is_a_problem(db, monkeypatch):
    monkeypatch.setattr(production_guard.os, "getenv", lambda k, default="": default)
    _seed(db)

    problems = production_guard.check(db, _Settings())
    assert any("BEDROCK_MODEL_ID" in p for p in problems)


def test_a_seeded_demo_credential_is_a_problem(db, monkeypatch):
    monkeypatch.setattr(production_guard.os, "getenv", lambda k, default="": "us.anthropic.claude-x" if k == "BEDROCK_MODEL_ID" else default)
    _seed(db, username="frontdesk", password_hash="pbkdf2_sha256$260000$riverbend02saltval0$abc")

    problems = production_guard.check(db, _Settings())
    assert any("demo seed credential" in p for p in problems)


def test_an_active_legacy_staff_account_is_a_problem(db, monkeypatch):
    monkeypatch.setattr(production_guard.os, "getenv", lambda k, default="": "us.anthropic.claude-x" if k == "BEDROCK_MODEL_ID" else default)
    _seed(db, role="staff")

    problems = production_guard.check(db, _Settings())
    assert any("'staff' role" in p for p in problems)


def test_an_unclassified_shared_mfa_account_is_a_problem_only_when_mfa_is_on(db, monkeypatch):
    monkeypatch.setattr(production_guard.os, "getenv", lambda k, default="": "us.anthropic.claude-x" if k == "BEDROCK_MODEL_ID" else default)
    _seed(db, mfa_shared_account=True)

    problems = production_guard.check(db, _Settings())
    assert any("mfa_shared_account" in p for p in problems)

    monkeypatch.setattr(app_mod.mfa_config, "effective_mode", lambda: "off")
    # 'off' is itself already flagged separately — this only proves the
    # shared-account check does not ALSO fire once MFA is off (nothing to
    # classify against if MFA isn't checked at all).
    problems_off = production_guard.check(db, _Settings())
    assert not any("mfa_shared_account" in p for p in problems_off)


def test_an_inactive_demo_or_staff_account_is_not_flagged(db, monkeypatch):
    """Disabled rows can't authenticate at all — no need to make production
    startup depend on cleaning up history that can never be reached."""
    monkeypatch.setattr(production_guard.os, "getenv", lambda k, default="": "us.anthropic.claude-x" if k == "BEDROCK_MODEL_ID" else default)
    _seed(db, username="frontdesk", role="staff", is_active=False, mfa_shared_account=True,
          password_hash="pbkdf2_sha256$260000$riverbend02saltval0$abc")

    problems = production_guard.check(db, _Settings())
    assert not any("'staff' role" in p or "mfa_shared_account" in p for p in problems)
