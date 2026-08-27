"""Loader + evaluator for config/mfa.yaml — the MFA rollout source of truth.

Mirrors roles_config.py's shape deliberately (parse once, cache for the
process lifetime, reload() for tests and the gateway's own startup check) —
same file, same reasoning: a config nobody re-reads per request but that
must fail loudly at startup if it's missing or malformed, not on the first
request that needs it.

mfa_requirement_for(user) is the one function app.py's /login route calls.
Everything else here is what that folds together: the configured mode, the
dated cutover, the emergency rollback override, the configured scope, and
the two per-account facts (mfa_shared_account, mfa_pilot) that no config
file can know without real staff-directory data — see migration 033's
comment on why those default the way they do.
"""
import datetime
import os
from typing import Optional

import yaml

_MFA_CONFIG_PATH = os.getenv(
    "MFA_CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "mfa.yaml"),
)

_VALID_MODES = ("off", "prompt", "enforce")
_VALID_SCOPES = ("pilot", "all")

_config: Optional[dict] = None


class MfaConfigError(Exception):
    """config/mfa.yaml is missing, malformed, or names a mode/scope this
    module doesn't recognize. Raised at load time so a bad deploy fails
    before accepting any traffic, the same posture app.py's lifespan check
    already applies to roles.yaml and INTERNAL_SERVICE_TOKEN."""


def _load() -> dict:
    global _config
    if _config is None:
        try:
            with open(_MFA_CONFIG_PATH) as f:
                raw = yaml.safe_load(f) or {}
        except OSError as exc:
            raise MfaConfigError(f"could not read {_MFA_CONFIG_PATH!r}: {exc}") from exc

        mode = raw.get("mode", "off")
        if mode is False:
            # YAML 1.1's most infamous footgun: an UNQUOTED `mode: off` in
            # the file parses as the boolean False, not the string "off" —
            # same for on/yes/no. Coerce the one case that matches this
            # module's own intent rather than failing confusingly on a
            # config file that reads correctly to a human.
            mode = "off"
        if mode not in _VALID_MODES:
            raise MfaConfigError(
                f"mfa.yaml mode={mode!r} must be one of {_VALID_MODES} "
                f"(quote it — mode: \"off\" — if that's what you meant; YAML "
                f"parses a bare off/on/yes/no as a boolean)"
            )

        scope = raw.get("scope", "pilot")
        if scope not in _VALID_SCOPES:
            raise MfaConfigError(f"mfa.yaml scope={scope!r} must be one of {_VALID_SCOPES}")

        cutover_raw = raw.get("cutover_at")
        cutover_at = None
        if cutover_raw:
            try:
                cutover_at = datetime.datetime.fromisoformat(str(cutover_raw).replace("Z", "+00:00"))
            except ValueError as exc:
                raise MfaConfigError(f"mfa.yaml cutover_at={cutover_raw!r} is not ISO 8601") from exc

        _config = {
            "mode": mode,
            "scope": scope,
            "cutover_at": cutover_at,
            "rollback_override": bool(raw.get("rollback_override", False)),
        }
    return _config


def reload() -> None:
    """Force the next call to re-read the file from disk. Used by the
    gateway's startup check and by tests; normal request handling relies on
    the cached, process-lifetime config — same contract as roles_config."""
    global _config
    _config = None


def config_path() -> str:
    return os.path.abspath(_MFA_CONFIG_PATH)


def raw() -> dict:
    """The parsed config, for read-only inspection (e.g. GET /mfa/status,
    the startup rollback-override log line). Callers must not mutate it."""
    return _load()


def effective_mode(*, now: Optional[datetime.datetime] = None) -> str:
    """The rollout mode after folding in the dated cutover and the
    emergency rollback override — before any per-account exemption
    (shared-account, pilot scope) is applied. See mfa_requirement_for for
    the full per-account decision."""
    cfg = _load()
    mode = cfg["mode"]

    if cfg["rollback_override"]:
        # The one override that always wins. "off" stays "off" — rollback
        # is a ceiling on enforcement, not a way to turn MFA ON for a
        # deployment that has it switched off entirely.
        return "off" if mode == "off" else "prompt"

    if mode == "prompt" and cfg["cutover_at"] is not None:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        if now >= cfg["cutover_at"]:
            return "enforce"

    return mode


def mfa_requirement_for(user, *, now: Optional[datetime.datetime] = None) -> str:
    """The ACTUAL behavior for this specific account: 'off', 'prompt', or
    'enforce'. Folds effective_mode() together with scope and the two
    per-account eligibility facts.

    `user` needs only three attributes: mfa_shared_account, mfa_pilot,
    role — a plain object or the SQLAlchemy User model both work, so tests
    can pass a lightweight stand-in without a database.
    """
    mode = effective_mode(now=now)
    if mode == "off":
        return "off"

    # Never silently — see migration 033's column comment and this file's
    # own module docstring. Every login for a shared account that would
    # otherwise be in scope logs why MFA was skipped (app.py's login route),
    # so the exemption is visible in the audit trail rather than invisible
    # in this function's return value alone.
    if getattr(user, "mfa_shared_account", True):
        return "off"

    cfg = _load()
    if cfg["scope"] == "pilot" and not getattr(user, "mfa_pilot", False):
        return "off"

    return mode
