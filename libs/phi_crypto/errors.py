"""Exception types for libs/phi_crypto.

Every message on every class here — base or subclass — must never include
plaintext PHI or raw key material. Messages may name an env var, a key
version identifier, or a generic description of what failed; never a value.
"""


class PhiCryptoError(Exception):
    """Base for every error this package raises."""


class KeyConfigurationError(PhiCryptoError):
    """A configured (or missing) key failed validation. Raised at service
    startup by EnvKeyProvider's constructor — never at request time — so a
    misconfigured deploy fails closed before accepting any traffic."""


class UnknownKeyVersionError(PhiCryptoError):
    """Stored ciphertext, a blind index, or a key-version column names a
    key version this process has no key configured for. Refuse rather than
    silently falling back to the active version or any other key."""


class EnvelopeFormatError(PhiCryptoError):
    """Stored ciphertext is not a well-formed phi_crypto envelope (bad
    base64, wrong length, unrecognized format marker) — data corruption or
    a caller passing something that was never produced by encrypt()."""


class DecryptionError(PhiCryptoError):
    """AEAD authentication failed. Deliberately does not distinguish wrong
    key vs. wrong AAD vs. tampered/corrupted ciphertext, to avoid turning
    the error message into an oracle for an attacker probing keys or AAD."""
