"""Loader for config/roles.yaml — records-service's own copy.

Copy-pasted from services/gateway/roles_config.py, per ADR 0001: there is no
shared Python library across services, so each service that needs the roles
config carries its own loader. The *config file* is shared (one
config/roles.yaml at the repo root, the client's signed permission matrix);
only the loader is duplicated.

Why this service needs it at all: the gateway's require_permission is the
first layer, not the boundary. This service's port is published, and the
client's direction was explicit — permissions are enforced at the data-query
boundary, not by hiding buttons. So the authorization path here checks the
caller's role itself rather than trusting that a gateway check happened.

The role is read from the database (users.role), never from a request header:
a header would be spoofable by anyone who can reach this port directly, which
is exactly the gap this closes.
"""
import os

import yaml

_ROLES_CONFIG_PATH = os.getenv(
    "ROLES_CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "roles.yaml"),
)

_config: dict | None = None


def _load() -> dict:
    global _config
    if _config is None:
        with open(_ROLES_CONFIG_PATH) as f:
            _config = yaml.safe_load(f) or {}
    return _config


def reload() -> None:
    """Force a re-read on the next call. Used by the startup check (so a
    missing file kills the process rather than the first request) and by
    tests; request handling relies on the cached, process-lifetime config."""
    global _config
    _config = None


def config_path() -> str:
    """Where the config is being read from — for startup diagnostics, so a
    packaging mistake reports the path it actually looked at."""
    return os.path.abspath(_ROLES_CONFIG_PATH)


def roles() -> dict:
    return _load().get("roles", {})


def permissions_for(role: str) -> set:
    """An unknown or absent role gets no permissions — fail closed. A role
    string that matches nothing in the matrix grants nothing, rather than
    falling back to a default."""
    role_def = roles().get(role)
    if not role_def:
        return set()
    return set(role_def.get("permissions", []))
