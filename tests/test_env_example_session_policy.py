"""`.env.example` must not silently override the session policy.

PR #30 review [high]: the code default moved to a 15-minute idle window, but
this template still set `SESSION_TIMEOUT_SECONDS=28800`. docker-compose loads
`.env`, and operators create `.env` from this template — so the env var won,
and every environment built that way kept an 8-hour idle window on shared
clinical workstations regardless of the code. A config template that quietly
reverses a security fix is worse than no template.

These tests pin the template to the code, in both directions.
"""
import os
import re

import pytest

from conftest import load_module

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_EXAMPLE = os.path.join(REPO, ".env.example")

settings = load_module("services/gateway/config.py", "gateway_config_for_env_check").settings


def _template_value(name):
    with open(ENV_EXAMPLE) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip()
    return None


@pytest.mark.parametrize(
    "env_var, code_default",
    [
        ("SESSION_TIMEOUT_SECONDS", lambda: settings.session_timeout_seconds),
        ("ABSOLUTE_SESSION_TIMEOUT_SECONDS", lambda: settings.absolute_session_timeout_seconds),
    ],
)
def test_template_matches_the_code_default(env_var, code_default):
    value = _template_value(env_var)

    assert value is not None, f"{env_var} is missing from .env.example"
    assert int(value) == code_default(), (
        f".env.example sets {env_var}={value} but the code default is "
        f"{code_default()} — compose loads .env, so the template would win"
    )


def test_the_idle_window_is_short_enough_for_a_shared_workstation():
    # The whole point of the fix. A regression here is not a style question.
    assert int(_template_value("SESSION_TIMEOUT_SECONDS")) <= 1800


def test_the_absolute_cap_does_not_exceed_one_shift():
    assert int(_template_value("ABSOLUTE_SESSION_TIMEOUT_SECONDS")) <= 28800


def test_the_idle_window_is_shorter_than_the_absolute_cap():
    # An idle window longer than the total lifetime would make the idle timeout
    # dead code — a real misconfiguration, not merely an odd one.
    assert int(_template_value("SESSION_TIMEOUT_SECONDS")) < int(
        _template_value("ABSOLUTE_SESSION_TIMEOUT_SECONDS")
    )
