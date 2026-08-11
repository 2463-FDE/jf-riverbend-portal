"""Loader for config/roles.yaml — production-readiness Stage 1 item 3.

Nothing in this repo read this file before now — it was pure documentation
of an intent the code didn't enforce. This module makes mfa_required a real,
live setting. (Stage 1 item 4 adds per-role permission enforcement on top of
this same file — expect this module to grow then.)

Parsed once per process and cached; call reload() (tests only) to force a
re-read after changing the file on disk.
"""
import os

import yaml

_ROLES_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "roles.yaml")

_config: dict | None = None


def _load() -> dict:
    global _config
    if _config is None:
        with open(_ROLES_CONFIG_PATH) as f:
            _config = yaml.safe_load(f) or {}
    return _config


def reload() -> None:
    """Force the next call to re-read the file from disk. Tests only —
    normal request handling relies on the cached, process-lifetime config."""
    global _config
    _config = None


def mfa_required() -> bool:
    return bool(_load().get("mfa_required", False))
