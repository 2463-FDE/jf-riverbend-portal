"""Unit tests for services/gateway/roles_config.py — the config/roles.yaml
loader that turned this file from documentation into the live RBAC
permission source.
"""
from conftest import load_module

roles_config = load_module("services/gateway/roles_config.py", "gateway_roles_config")


def test_reads_permissions_from_the_real_config_file():
    roles_config.reload()
    # The legacy staff role is still defined and still carries its original
    # full permission set — the P1 roster migration (w8-planner-2) moved ten
    # of the thirteen seeded staff accounts off it onto explicit roles;
    # frontdesk/labtech/itadmin remain on it, left for a human decision
    # (shared logins, a departed contractor) rather than auto-migrated.
    assert "patients.read" in roles_config.permissions_for("staff")


def test_unknown_role_gets_no_permissions_fail_closed():
    roles_config.reload()
    assert roles_config.permissions_for("not-a-real-role") == set()
    assert roles_config.permissions_for("") == set()
    assert roles_config.permissions_for(None) == set()


def test_no_default_role_fallback_is_declared():
    # P1 identity foundation (w8-planner-2): a `default_role: staff` key
    # previously sat in roles.yaml unread by any code — an unused fallback
    # that could silently restore flat staff access if something were later
    # wired up to it without checking whether that was ever the intended
    # behavior. Removed; this guards against it quietly coming back. Checks
    # the parsed YAML's top-level keys, not raw text — this file's own
    # comments are allowed to mention "default_role" while explaining why.
    import yaml

    roles_config.reload()
    with open(roles_config.config_path()) as f:
        config = yaml.safe_load(f)
    assert "default_role" not in config


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
