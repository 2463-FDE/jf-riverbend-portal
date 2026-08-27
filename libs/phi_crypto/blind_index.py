"""Deterministic blind-index calculation — HMAC-SHA256 keyed under
PHI_BLIND_INDEX_KEY_V<n>, never the encryption key.

encrypt() (envelope.py) is randomized on purpose: the same plaintext
encrypted twice produces different ciphertext, so equality can't be tested
on it directly. A blind index trades some of that guarantee for exact-match
lookups: HMAC is deterministic (same input + same key -> same output), so
two rows with the same normalized SSN get the same blind-index value and a
`WHERE match_key = :computed_value` query still works — while a reader
without PHI_BLIND_INDEX_KEY_V<n> still can't reverse a hex digest back to a
digit string. It is intentionally a *different* key from the encryption
key (see keys.py's EnvKeyProvider, which refuses to start if they match)
so that compromising one does not automatically compromise the other.

normalize_ssn must be the ONLY place SSN normalization happens — write and
lookup paths both call it before compute_blind_index, so the same raw
input always produces the same normalized value and therefore the same
blind index. A second, drifted normalization implementation would silently
break every future lookup for records written under the first one.
"""
import hashlib
import hmac
import re
from typing import Optional

from .errors import EnvelopeFormatError

_KEY_LEN = 32


def normalize_ssn(raw: Optional[str]) -> Optional[str]:
    """Digits only, or None if there are none — mirrors the digit-only
    comparison _normalize_ssn already performed in intake-service/
    records-service before this package existed (adr/0004, migration 015)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits or None


def compute_blind_index(normalized_value: str, key: bytes) -> str:
    """HMAC-SHA256(key, normalized_value), hex-encoded. `normalized_value`
    must already be normalized (see normalize_ssn) — this function does not
    normalize on the caller's behalf, so a caller comparing a blind index
    computed here against one stored earlier must be certain both went
    through the same normalization first."""
    if len(key) != _KEY_LEN:
        raise EnvelopeFormatError(f"blind-index key must be exactly {_KEY_LEN} bytes")
    return hmac.new(key, normalized_value.encode("utf-8"), hashlib.sha256).hexdigest()
