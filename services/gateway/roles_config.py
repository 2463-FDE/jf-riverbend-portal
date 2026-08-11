"""Loader for config/roles.yaml — production-readiness Stage 1, items 3-4.

Nothing in this repo read this file before Stage 1 item 3 — it was pure
documentation of an intent the code didn't enforce. mfa_required (item 3)
was the first real setting; permissions_for (item 4) is the second —
app.py's require_permission dependency uses it to gate routes per-role
instead of "any authenticated staff session" for everything.

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


def roles() -> dict:
    return _load().get("roles", {})


def permissions_for(role: str) -> set:
    """An unknown role gets no permissions — fail closed, not open, for a
    role string that doesn't match anything defined in roles.yaml."""
    role_def = roles().get(role)
    if not role_def:
        return set()
    return set(role_def.get("permissions", []))
