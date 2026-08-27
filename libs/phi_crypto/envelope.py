"""Versioned AEAD envelope encode/decode — AES-256-GCM via `cryptography`.

An envelope encodes (format marker, nonce, ciphertext+tag) as one base64
TEXT-safe string. The format marker versions the *encoding scheme itself*
(nonce length, AEAD algorithm) — it is deliberately separate from the *key
version* (which of PHI_ENCRYPTION_KEY_V1/V2/... encrypted this value),
which callers store in a sibling column and pass in explicitly on both
encrypt and decrypt (see libs/phi_crypto/keys.py, adr/0009). A future
change to the AEAD algorithm bumps the format marker; a key rotation
bumps PHI_ACTIVE_KEY_VERSION — the two rotate independently.

`aad` (additional authenticated data) is required on both calls, not
optional: every caller in this codebase binds it to the specific row and
column being encrypted (e.g. b"patients.ssn.1042"), so ciphertext from one
row/column can never be decrypted as if it belonged to another — a copy-
paste or row-substitution attack fails the AEAD tag check instead of
silently succeeding.
"""
import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import DecryptionError, EnvelopeFormatError

_FORMAT_MARKER = b"phc1"  # phi_crypto envelope format 1: AES-256-GCM, 12-byte nonce
_NONCE_LEN = 12
_KEY_LEN = 32


def encrypt(plaintext: str, key: bytes, aad: bytes) -> str:
    """Encrypt `plaintext` under `key` (exactly 32 bytes), bound to `aad`.
    Returns a base64 envelope string suitable for a TEXT column. A fresh
    random nonce is generated per call — the same plaintext encrypted twice
    produces different ciphertext, by design (this is not a blind index;
    see blind_index.py for the deterministic, searchable counterpart)."""
    if len(key) != _KEY_LEN:
        raise EnvelopeFormatError(f"encryption key must be exactly {_KEY_LEN} bytes")

    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), aad)
    return base64.b64encode(_FORMAT_MARKER + nonce + ciphertext).decode("ascii")


def decrypt(envelope: str, key: bytes, aad: bytes) -> str:
    """Decrypt an envelope produced by encrypt(), under `key` and `aad`.
    Raises EnvelopeFormatError for malformed input (not a phi_crypto
    envelope at all) and DecryptionError for a well-formed envelope that
    fails AEAD authentication (wrong key, wrong aad, or tampered
    ciphertext) — callers should treat both as fatal, not retry with a
    guessed alternative key or aad."""
    if len(key) != _KEY_LEN:
        raise EnvelopeFormatError(f"decryption key must be exactly {_KEY_LEN} bytes")

    try:
        raw = base64.b64decode(envelope, validate=True)
    except Exception as exc:
        raise EnvelopeFormatError("envelope is not valid base64") from exc

    marker_len = len(_FORMAT_MARKER)
    if len(raw) < marker_len + _NONCE_LEN:
        raise EnvelopeFormatError("envelope is too short to be valid")

    marker = raw[:marker_len]
    if marker != _FORMAT_MARKER:
        raise EnvelopeFormatError("unrecognized envelope format marker")

    nonce = raw[marker_len : marker_len + _NONCE_LEN]
    ciphertext = raw[marker_len + _NONCE_LEN :]

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptionError("PHI envelope failed authentication") from exc

    return plaintext.decode("utf-8")
