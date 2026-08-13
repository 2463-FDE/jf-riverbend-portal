"""Unit tests for services/gateway/roles_config.py — the config/roles.yaml
loader that turned this file from documentation into the live RBAC
permission source.
"""
from conftest import load_module

roles_config = load_module("services/gateway/roles_config.py", "gateway_roles_config")


def test_reads_permissions_from_the_real_config_file():
    roles_config.reload()
    # The legacy staff role is still defined and still carries its original
    # full permission set — every existing account is on it until the
    # roster-driven migration runs.
    assert "patients.read" in roles_config.permissions_for("staff")


def test_unknown_role_gets_no_permissions_fail_closed():
    roles_config.reload()
    assert roles_config.permissions_for("not-a-real-role") == set()
    assert roles_config.permissions_for("") == set()
    assert roles_config.permissions_for(None) == set()


def test_config_is_cached_until_reload(monkeypatch, tmp_path):
    roles_config.reload()
    assert "patients.read" in roles_config.permissions_for("staff")

    fake_path = tmp_path / "roles.yaml"
    fake_path.write_text("roles:\n  staff:\n    permissions:\n      - only.this\n")
    monkeypatch.setattr(roles_config, "_ROLES_CONFIG_PATH", str(fake_path))

    # Still the real file's contents — already parsed and cached.
    assert "patients.read" in roles_config.permissions_for("staff")

    roles_config.reload()
    assert roles_config.permissions_for("staff") == {"only.this"}
