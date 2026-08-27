"""KeyProvider protocol + an environment-backed implementation.

This repo has no KMS/secrets-manager integration to build on (w8-planner-2
P2, adr/0009) — EnvKeyProvider reads keys from the process environment,
which is a real but explicitly non-production-grade posture: it is
"environment-backed secret injection suitable for the current deployment
model," not KMS-backed key custody. Every caller depends on the KeyProvider
*protocol*, never on EnvKeyProvider or an env var name directly, so a
future KMS-backed provider can replace this one without touching
intake-service or records-service's own code.

Env vars (see .env.example):
    PHI_ACTIVE_KEY_VERSION=v1
    PHI_ENCRYPTION_KEY_V1=<base64, decodes to exactly 32 bytes>
    PHI_BLIND_INDEX_KEY_V1=<base64, decodes to exactly 32 bytes, != the above>

A version other than the active one may also be configured (e.g.
PHI_ENCRYPTION_KEY_V0 / PHI_BLIND_INDEX_KEY_V0) to keep decrypting data
written under a superseded key during a rotation — new writes always use
PHI_ACTIVE_KEY_VERSION; reads select a key by whatever version is stored
alongside the ciphertext/blind index being read (see adr/0009). A version
never configured here raises UnknownKeyVersionError rather than silently
falling back to another key.
"""
import base64
import os
import re
from typing import Dict, Optional, Protocol

from .errors import KeyConfigurationError, UnknownKeyVersionError

_KEY_LEN = 32
# Captures the FULL version suffix (e.g. "V1"), not just the digit — the
# version identifier itself (PHI_ACTIVE_KEY_VERSION="v1") already includes
# the leading "v", so the env var name is built as
# f"PHI_ENCRYPTION_KEY_{version.upper()}", not with an extra literal "V".
_ENCRYPTION_VAR_RE = re.compile(r"^PHI_ENCRYPTION_KEY_([A-Za-z0-9]+)$")
_BLIND_INDEX_VAR_RE = re.compile(r"^PHI_BLIND_INDEX_KEY_([A-Za-z0-9]+)$")


class KeyProvider(Protocol):
    def active_key_version(self) -> str: ...

    def encryption_key(self, version: str) -> bytes: ...

    def blind_index_key(self, version: str) -> bytes: ...


def _decode_key(var_name: str, raw: Optional[str]) -> bytes:
    if not raw:
        raise KeyConfigurationError(f"{var_name} is not set")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise KeyConfigurationError(f"{var_name} is not valid base64") from exc
    if len(decoded) != _KEY_LEN:
        raise KeyConfigurationError(f"{var_name} must decode to exactly {_KEY_LEN} bytes")
    return decoded


class EnvKeyProvider:
    """Validates every configured key-version pair eagerly at construction
    time — not just the active version — so a malformed predecessor key
    (kept around for rotation) fails service startup immediately instead of
    surfacing later as a decrypt-time 500 the first time something needs
    it. Construct once per process, at startup, the same way every other
    service in this repo validates its own required config up front."""

    def __init__(self, env: Optional[Dict[str, str]] = None):
        env = env if env is not None else os.environ

        active_raw = env.get("PHI_ACTIVE_KEY_VERSION")
        if not active_raw:
            raise KeyConfigurationError("PHI_ACTIVE_KEY_VERSION is not set")
        active = active_raw.lower()

        # Version identifiers are matched case-insensitively: "v1" (the
        # convention PHI_ACTIVE_KEY_VERSION uses) and the "V1" suffix on
        # PHI_ENCRYPTION_KEY_V1 refer to the same version.
        versions = set()
        for name in env:
            m = _ENCRYPTION_VAR_RE.match(name)
            if m:
                versions.add(m.group(1).lower())
            m = _BLIND_INDEX_VAR_RE.match(name)
            if m:
                versions.add(m.group(1).lower())

        if active not in versions:
            raise KeyConfigurationError(
                f"PHI_ACTIVE_KEY_VERSION={active_raw!r} has no matching "
                f"PHI_ENCRYPTION_KEY_*/PHI_BLIND_INDEX_KEY_* pair configured"
            )

        encryption_keys: Dict[str, bytes] = {}
        blind_index_keys: Dict[str, bytes] = {}
        for version in versions:
            enc_var = f"PHI_ENCRYPTION_KEY_{version.upper()}"
            idx_var = f"PHI_BLIND_INDEX_KEY_{version.upper()}"
            enc_key = _decode_key(enc_var, env.get(enc_var))
            idx_key = _decode_key(idx_var, env.get(idx_var))
            if enc_key == idx_key:
                raise KeyConfigurationError(f"{enc_var} and {idx_var} must not be identical")
            encryption_keys[version] = enc_key
            blind_index_keys[version] = idx_key

        self._active = active
        self._encryption_keys = encryption_keys
        self._blind_index_keys = blind_index_keys

    def active_key_version(self) -> str:
        return self._active

    def encryption_key(self, version: str) -> bytes:
        try:
            return self._encryption_keys[version.lower()]
        except KeyError:
            raise UnknownKeyVersionError(f"no encryption key configured for version {version!r}") from None

    def blind_index_key(self, version: str) -> bytes:
        try:
            return self._blind_index_keys[version.lower()]
        except KeyError:
            raise UnknownKeyVersionError(f"no blind-index key configured for version {version!r}") from None
