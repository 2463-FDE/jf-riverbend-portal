"""Unit tests for services/gateway/roles_config.py — the config/roles.yaml
loader that production-readiness Stage 1 item 3 (mfa_required) added.
"""
from conftest import load_module

roles_config = load_module("services/gateway/roles_config.py", "gateway_roles_config")


def test_mfa_required_reflects_the_real_config_file():
    # config/roles.yaml ships with mfa_required: false — see that file's
    # comment for why this isn't flipped to true in this change.
    roles_config.reload()
    assert roles_config.mfa_required() is False


def test_mfa_required_is_cached_until_reload(monkeypatch, tmp_path):
    roles_config.reload()
    assert roles_config.mfa_required() is False

    fake_path = tmp_path / "roles.yaml"
    fake_path.write_text("mfa_required: true\n")
    monkeypatch.setattr(roles_config, "_ROLES_CONFIG_PATH", str(fake_path))

    # Still false — the real file was already parsed and cached.
    assert roles_config.mfa_required() is False

    roles_config.reload()
    assert roles_config.mfa_required() is True
