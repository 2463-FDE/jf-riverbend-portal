"""Loader for config/roles.yaml — the live RBAC permission source.

Nothing in this repo read this file before now — it was pure documentation
of an intent the code didn't enforce. permissions_for is what makes it real:
app.py's require_permission dependency uses it to gate routes per-role
instead of accepting "any authenticated staff session" for everything.

Gateway-route gating is only the first layer. Per the client's 2026-08-12
direction, the authoritative check belongs at the data-query boundary
(records-service, alongside patient_access_gate.py) — several domain
services are still reachable directly on published ports with no gateway
trust check at all, so this file alone does not make permissions
unbypassable. See the current cycle's plan.

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


def roles() -> dict:
    return _load().get("roles", {})


def permissions_for(role: str) -> set:
    """An unknown role gets no permissions — fail closed, not open, for a
    role string that doesn't match anything defined in roles.yaml."""
    role_def = roles().get(role)
    if not role_def:
        return set()
    return set(role_def.get("permissions", []))
