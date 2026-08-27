"""AEAD encryption for TOTP secrets — MFA-specific key material.

Deliberately NOT libs/phi_crypto's PHI_ACTIVE_KEY_VERSION /
PHI_ENCRYPTION_KEY_V<n> / EnvKeyProvider: a TOTP secret and PHI are
different data classes, and reusing the PHI keys would silently couple this
service's MFA posture to PHI key rotation (and vice versa) — a future PHI
key rotation would force a TOTP secret re-encryption nobody asked for, and
a compromise of one key class would immediately implicate the other. This
module owns its own env vars (MFA_ACTIVE_KEY_VERSION / MFA_ENCRYPTION_KEY_
V<n>) and its own fail-closed validation, structured the same way
libs/phi_crypto/keys.py validates PHI keys — but it deliberately reuses the
generic AEAD envelope ENCODE/DECODE routine from libs/phi_crypto/envelope.py
(the algorithm and on-disk format, not the key material) rather than
reimplementing AES-256-GCM framing a second time in this service.

Fail closed: get_key_provider() raises MfaKeyConfigurationError the first
time anything tries to encrypt or decrypt a TOTP secret without
MFA_ACTIVE_KEY_VERSION and a matching MFA_ENCRYPTION_KEY_V<n> configured.
app.py's startup check (mirroring its existing INTERNAL_SERVICE_TOKEN/
roles.yaml checks) calls this eagerly whenever config/mfa.yaml's mode is
not "off", so a misconfigured deploy refuses to start rather than accepting
logins and failing the first time someone tries to enroll.
"""
import base64
import os
import re
from typing import Dict, Optional, Tuple

from libs.phi_crypto.envelope import decrypt as _aead_decrypt
from libs.phi_crypto.envelope import encrypt as _aead_encrypt

_KEY_LEN = 32
_ENCRYPTION_VAR_RE = re.compile(r"^MFA_ENCRYPTION_KEY_([A-Za-z0-9]+)$")


class MfaCryptoError(Exception):
    """Base for every error this module raises. Never carries plaintext
    secret material, key bytes, or a TOTP/backup code in its message."""


class MfaKeyConfigurationError(MfaCryptoError):
    """A configured (or missing) MFA key failed validation."""


class MfaUnknownKeyVersionError(MfaCryptoError):
    """Stored ciphertext names a key version this process has no key
    configured for. Refuse rather than silently falling back to the active
    version or any other key."""


def _decode_key(var_name: str, raw: Optional[str]) -> bytes:
    if not raw:
        raise MfaKeyConfigurationError(f"{var_name} is not set")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise MfaKeyConfigurationError(f"{var_name} is not valid base64") from exc
    if len(decoded) != _KEY_LEN:
        raise MfaKeyConfigurationError(f"{var_name} must decode to exactly {_KEY_LEN} bytes")
    return decoded


class MfaEnvKeyProvider:
    """Validates every configured key-version eagerly at construction time
    — not just the active version — so a malformed predecessor key (kept
    around for rotation) fails startup immediately rather than surfacing as
    a decrypt-time 500 the first time something needs it."""

    def __init__(self, env: Optional[Dict[str, str]] = None):
        env = env if env is not None else os.environ

        active_raw = env.get("MFA_ACTIVE_KEY_VERSION")
        if not active_raw:
            raise MfaKeyConfigurationError("MFA_ACTIVE_KEY_VERSION is not set")
        active = active_raw.lower()

        versions = set()
        for name in env:
            m = _ENCRYPTION_VAR_RE.match(name)
            if m:
                versions.add(m.group(1).lower())

        if active not in versions:
            raise MfaKeyConfigurationError(
                f"MFA_ACTIVE_KEY_VERSION={active_raw!r} has no matching MFA_ENCRYPTION_KEY_* configured"
            )

        keys: Dict[str, bytes] = {}
        for version in versions:
            var = f"MFA_ENCRYPTION_KEY_{version.upper()}"
            keys[version] = _decode_key(var, env.get(var))

        self._active = active
        self._keys = keys

    def active_key_version(self) -> str:
        return self._active

    def encryption_key(self, version: str) -> bytes:
        try:
            return self._keys[version.lower()]
        except KeyError:
            raise MfaUnknownKeyVersionError(f"no MFA encryption key configured for version {version!r}") from None


_key_provider: Optional[MfaEnvKeyProvider] = None


def get_key_provider() -> MfaEnvKeyProvider:
    """Constructed once per process, lazily, and cached — same pattern as
    every other fail-closed config in this service. Call reset_key_provider
    in tests to force reconstruction against a different environment."""
    global _key_provider
    if _key_provider is None:
        _key_provider = MfaEnvKeyProvider()
    return _key_provider


def reset_key_provider() -> None:
    """Tests only — forces the next get_key_provider() call to reconstruct
    from the current environment."""
    global _key_provider
    _key_provider = None


def _aad(user_id: int) -> bytes:
    # Binds ciphertext to the specific account it belongs to, the same way
    # every libs/phi_crypto caller binds AAD to a row/column — a copy-paste
    # of one user's encrypted secret into another user's row fails AEAD
    # authentication instead of silently decrypting.
    return f"users.mfa_secret.{user_id}".encode("utf-8")


def encrypt_totp_secret(user_id: int, secret: str) -> Tuple[str, str]:
    """Returns (envelope, key_version) for storage in
    users.mfa_secret_ciphertext / mfa_secret_key_version."""
    kp = get_key_provider()
    version = kp.active_key_version()
    envelope = _aead_encrypt(secret, kp.encryption_key(version), _aad(user_id))
    return envelope, version


def decrypt_totp_secret(user_id: int, envelope: str, key_version: str) -> str:
    kp = get_key_provider()
    return _aead_decrypt(envelope, kp.encryption_key(key_version), _aad(user_id))
