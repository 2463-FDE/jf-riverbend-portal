"""Shared PHI field-level cryptography primitives (w8-planner-2 P2, adr/0009).

Supersedes ADR 0001's "no shared Python library across services" for this
one, security-critical case: crypto primitives copy-pasted across 4+
services drift — inconsistent key validation, incompatible ciphertext
formats between services that must decrypt what another wrote. Everything
else stays copy-paste, per ADR 0001; only this narrow surface is shared.

What lives here (deliberately small — see adr/0009 for the boundary):
    encrypt / decrypt          AEAD (AES-256-GCM) envelope encode/decode
    compute_blind_index        deterministic HMAC-SHA256 match key
    normalize_ssn               the one place SSN normalization happens
    KeyProvider / EnvKeyProvider   key loading + fail-closed validation
    PhiCryptoError and subclasses  exceptions that never carry PHI or keys

What does NOT live here — stays in each consuming service, same as every
other per-service concern in this codebase: FastAPI startup hooks,
SQLAlchemy models/sessions, migrations, authorization, HTTP handling,
config classes, and payload logging. This package has no framework
dependency and no I/O beyond reading os.environ in EnvKeyProvider.

There is deliberately no SQLAlchemy TypeDecorator here — encrypting a PHI
field needs an explicit AAD bound to the row (see envelope.py), an
explicit key version to persist alongside the ciphertext, and — for SSN
only — an explicit blind index. A transparent column type would hide all
three behind ordinary attribute assignment. Callers instead do, explicitly,
in their own service code: normalize -> encrypt -> (compute a blind index,
if the field is looked up by equality) -> persist envelope + key version +
blind index as separate columns.
"""
from .blind_index import compute_blind_index, normalize_ssn
from .envelope import decrypt, encrypt
from .errors import (
    DecryptionError,
    EnvelopeFormatError,
    KeyConfigurationError,
    PhiCryptoError,
    UnknownKeyVersionError,
)
from .keys import EnvKeyProvider, KeyProvider

__all__ = [
    "encrypt",
    "decrypt",
    "compute_blind_index",
    "normalize_ssn",
    "KeyProvider",
    "EnvKeyProvider",
    "PhiCryptoError",
    "KeyConfigurationError",
    "UnknownKeyVersionError",
    "EnvelopeFormatError",
    "DecryptionError",
]
