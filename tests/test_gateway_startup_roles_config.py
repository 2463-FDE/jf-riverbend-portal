"""The gateway must refuse to start if it cannot load config/roles.yaml.

PR #26 review, [critical]: `roles_config` loads the file lazily on the first
require_permission call. The gateway's Docker build context used to be
./services/gateway with `COPY . .`, so repo-root config/roles.yaml never
reached the image — the container started, /healthz and /login passed, and then
every authenticated route raised FileNotFoundError and returned 500. Local
pytest could not catch it because tests run from the repo root, where the
relative path resolves.

The build context is fixed (see services/gateway/Dockerfile), and these tests
cover the second half: a deployment that still cannot see the file fails at
startup with a message naming the path, instead of degrading into per-request
500s. Uses `with TestClient(...)` deliberately — Starlette only runs lifespan
for a context-managed client, which is why the other gateway tests (which do
not want lifespan) construct the client bare.
"""
import pytest
from fastapi.testclient import TestClient

from conftest import load_module

app_mod = load_module("services/gateway/app.py", "gateway_app_startup")

GOOD_TOKEN = "test-internal-token-abc123-well-over-the-32-char-floor"


def test_starts_when_the_roles_config_is_present(monkeypatch):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", GOOD_TOKEN)
    app_mod.roles_config.reload()

    with TestClient(app_mod.app) as client:
        assert client.get("/healthz").status_code == 200


def test_refuses_to_start_when_the_roles_config_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod.settings, "internal_service_token", GOOD_TOKEN)
    monkeypatch.setattr(
        app_mod.roles_config, "_ROLES_CONFIG_PATH", str(tmp_path / "nope.yaml")
    )
    app_mod.roles_config.reload()

    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(app_mod.app):
            pass

    message = str(excinfo.value)
    assert "roles" in message.lower()
    assert "nope.yaml" in message  # names the path it actually looked at
    app_mod.roles_config.reload()


def test_refuses_to_start_when_the_roles_config_defines_no_roles(monkeypatch, tmp_path):
    # A present-but-empty file is just as fatal: every role would resolve to no
    # permissions and every authorized route would 403. Fail loudly instead.
    empty = tmp_path / "roles.yaml"
    empty.write_text("roles: {}\n")
    monkeypatch.setattr(app_mod.settings, "internal_service_token", GOOD_TOKEN)
    monkeypatch.setattr(app_mod.roles_config, "_ROLES_CONFIG_PATH", str(empty))
    app_mod.roles_config.reload()

    with pytest.raises(RuntimeError, match="(?i)roles"):
        with TestClient(app_mod.app):
            pass

    app_mod.roles_config.reload()


def test_config_path_is_absolute_for_diagnostics(monkeypatch):
    monkeypatch.setattr(app_mod.roles_config, "_ROLES_CONFIG_PATH", "config/roles.yaml")

    assert app_mod.roles_config.config_path().startswith("/")
