"""Unit tests for services/gateway/mfa_config.py — the rollout mode/scope
decision, independent of any HTTP route or database.
"""
import datetime

import pytest

from conftest import load_module

mfa_config = load_module("services/gateway/mfa_config.py", "gateway_mfa_config")


class _User:
    def __init__(self, *, mfa_shared_account=False, mfa_pilot=False, role="clinician"):
        self.mfa_shared_account = mfa_shared_account
        self.mfa_pilot = mfa_pilot
        self.role = role


def _write_config(tmp_path, monkeypatch, text: str):
    path = tmp_path / "mfa.yaml"
    path.write_text(text)
    monkeypatch.setattr(mfa_config, "_MFA_CONFIG_PATH", str(path))
    mfa_config.reload()
    return path


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    yield
    mfa_config.reload()


# --- basic loading / validation ---------------------------------------------


def test_defaults_are_off_and_pilot(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: off\n")
    assert mfa_config.effective_mode() == "off"


def test_rejects_an_unrecognized_mode(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: sometimes\n")
    with pytest.raises(mfa_config.MfaConfigError):
        mfa_config.effective_mode()


def test_rejects_an_unrecognized_scope(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: enforce\nscope: everyone\n")
    with pytest.raises(mfa_config.MfaConfigError):
        mfa_config.mfa_requirement_for(_User())


def test_missing_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(mfa_config, "_MFA_CONFIG_PATH", str(tmp_path / "does-not-exist.yaml"))
    mfa_config.reload()
    with pytest.raises(mfa_config.MfaConfigError):
        mfa_config.effective_mode()


# --- mode off/prompt/enforce, scope pilot/all -------------------------------


def test_mode_off_means_off_for_everyone(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: off\nscope: all\n")
    assert mfa_config.mfa_requirement_for(_User(mfa_pilot=True)) == "off"
    assert mfa_config.mfa_requirement_for(_User(mfa_pilot=False)) == "off"


def test_pilot_scope_excludes_non_pilot_accounts(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: enforce\nscope: pilot\n")
    assert mfa_config.mfa_requirement_for(_User(mfa_pilot=False)) == "off"
    assert mfa_config.mfa_requirement_for(_User(mfa_pilot=True)) == "enforce"


def test_scope_all_covers_every_non_shared_account(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: enforce\nscope: all\n")
    assert mfa_config.mfa_requirement_for(_User(mfa_pilot=False)) == "enforce"
    assert mfa_config.mfa_requirement_for(_User(mfa_pilot=True)) == "enforce"


def test_prompt_mode_is_reported_for_in_scope_accounts(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: prompt\nscope: pilot\n")
    assert mfa_config.mfa_requirement_for(_User(mfa_pilot=True)) == "prompt"


# --- shared accounts are NEVER prompted or enforced, regardless of scope ---


def test_shared_account_is_exempt_even_under_scope_all(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: enforce\nscope: all\n")
    assert mfa_config.mfa_requirement_for(_User(mfa_shared_account=True)) == "off"


def test_shared_account_is_exempt_even_when_also_marked_pilot(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: enforce\nscope: pilot\n")
    user = _User(mfa_shared_account=True, mfa_pilot=True)
    assert mfa_config.mfa_requirement_for(user) == "off"


# --- dated cutover: prompt -> enforce automatically once reached -----------


def test_cutover_not_yet_reached_stays_prompt(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        "mode: prompt\nscope: all\ncutover_at: \"2099-01-01T00:00:00Z\"\n",
    )
    assert mfa_config.effective_mode() == "prompt"


def test_cutover_reached_becomes_enforce(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        "mode: prompt\nscope: all\ncutover_at: \"2020-01-01T00:00:00Z\"\n",
    )
    assert mfa_config.effective_mode() == "enforce"


def test_cutover_is_ignored_when_mode_is_not_prompt(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        "mode: off\ncutover_at: \"2020-01-01T00:00:00Z\"\n",
    )
    assert mfa_config.effective_mode() == "off"


def test_cutover_can_be_evaluated_against_an_explicit_now(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        "mode: prompt\ncutover_at: \"2026-06-01T00:00:00Z\"\n",
    )
    before = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
    after = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    assert mfa_config.effective_mode(now=before) == "prompt"
    assert mfa_config.effective_mode(now=after) == "enforce"


# --- emergency rollback override --------------------------------------------


def test_rollback_override_forces_prompt_even_past_cutover(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        "mode: prompt\ncutover_at: \"2020-01-01T00:00:00Z\"\nrollback_override: true\n",
    )
    assert mfa_config.effective_mode() == "prompt"


def test_rollback_override_forces_prompt_from_enforce(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: enforce\nrollback_override: true\n")
    assert mfa_config.effective_mode() == "prompt"


def test_rollback_override_does_not_turn_mfa_on_when_mode_is_off(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "mode: off\nrollback_override: true\n")
    assert mfa_config.effective_mode() == "off"


def test_reload_picks_up_a_changed_file(tmp_path, monkeypatch):
    path = _write_config(tmp_path, monkeypatch, "mode: off\n")
    assert mfa_config.effective_mode() == "off"
    path.write_text("mode: enforce\nscope: all\n")
    mfa_config.reload()
    assert mfa_config.effective_mode() == "enforce"
