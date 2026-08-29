"""services/gateway/app.py's lifespan — the production_guard wiring itself.

Mirrors test_gateway_startup_roles_config.py's pattern: `with TestClient(...)`
is required because Starlette only runs lifespan startup/shutdown for a
context-managed client.
"""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_boot_guard")

GOOD_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def _sqlite_session_local():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    app_mod.User.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", GOOD_TOKEN)
    app_mod.roles_config.reload()
    monkeypatch.setattr(app_mod.mfa_config, "effective_mode", lambda: "enforce")
    monkeypatch.setattr(app_mod.settings, "payer_integration_mode", "live")
    monkeypatch.setattr(app_mod.settings, "payer_api_key", "a-real-key")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-x")
    monkeypatch.setenv("MFA_ACTIVE_KEY_VERSION", "v1")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY_V1", base64.b64encode(b"\x01" * 32).decode())
    app_mod.mfa_crypto.reset_key_provider()


def test_development_mode_never_runs_the_guard_even_with_an_unsafe_posture(monkeypatch):
    # ENVIRONMENT defaults to "development" — an unsafe posture (MFA off) must
    # not block local/dev/test startup, only a real production deployment.
    monkeypatch.setattr(app_mod.mfa_config, "effective_mode", lambda: "off")

    with TestClient(app_mod.app) as client:
        assert client.get("/healthz").status_code == 200


def test_production_mode_starts_when_the_posture_is_safe(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "environment", "production")
    monkeypatch.setattr(app_mod, "SessionLocal", _sqlite_session_local())

    with TestClient(app_mod.app) as client:
        assert client.get("/healthz").status_code == 200


def test_production_mode_refuses_to_start_with_mfa_off(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "environment", "production")
    monkeypatch.setattr(app_mod, "SessionLocal", _sqlite_session_local())
    monkeypatch.setattr(app_mod.mfa_config, "effective_mode", lambda: "off")

    with pytest.raises(RuntimeError, match="MFA"):
        with TestClient(app_mod.app):
            pass


def test_production_mode_refuses_to_start_with_a_seeded_demo_account(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "environment", "production")
    Session = _sqlite_session_local()
    monkeypatch.setattr(app_mod, "SessionLocal", Session)
    with Session() as s:
        s.add(app_mod.User(
            username="frontdesk", password_hash="pbkdf2_sha256$260000$riverbend02saltval0$abc",
            role="staff", is_active=True,
        ))
        s.commit()

    with pytest.raises(RuntimeError, match="demo seed credential"):
        with TestClient(app_mod.app):
            pass
