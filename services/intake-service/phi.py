"""intake-service's own wiring onto libs/phi_crypto (adr/0012). Copy-pasted
into records-service/phi.py too, deliberately — see that file's docstring
and adr/0012 for why the primitives are shared but this wiring isn't.

get_key_provider() constructs EnvKeyProvider lazily, on first call, and
caches it — NOT at module import time. Importing this module (or app.py,
which the CI import-smoke-test and every unit test do) must not require
PHI_ACTIVE_KEY_VERSION/PHI_ENCRYPTION_KEY_V*/PHI_BLIND_INDEX_KEY_V* to be
set; only actually calling get_key_provider() does, and app.py's own
startup hook is what forces that call before this service accepts any
request — mirroring exactly how _fail_fast_on_an_unusable_token validates
INTERNAL_SERVICE_TOKEN at startup, not at import time.
"""
from typing import Optional, Tuple

from libs.phi_crypto import EnvKeyProvider, KeyProvider, compute_blind_index, decrypt, encrypt, normalize_ssn

_key_provider: Optional[KeyProvider] = None


def get_key_provider() -> KeyProvider:
    global _key_provider
    if _key_provider is None:
        _key_provider = EnvKeyProvider()
    return _key_provider


def _aad(column: str, patient_id: int) -> bytes:
    return f"patients.{column}.{patient_id}".encode("utf-8")


def encrypt_patient_field(patient_id: int, column: str, plaintext: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Returns (envelope, key_version) for a non-empty plaintext, or
    (None, None) unchanged — a patient row with no ssn/dob/notes supplied
    stays NULL, not an envelope of an empty string."""
    if not plaintext:
        return None, None
    kp = get_key_provider()
    version = kp.active_key_version()
    envelope = encrypt(plaintext, kp.encryption_key(version), _aad(column, patient_id))
    return envelope, version


def decrypt_patient_field(patient_id: int, column: str, envelope: Optional[str], key_version: Optional[str]) -> Optional[str]:
    """The inverse of encrypt_patient_field. A NULL envelope decrypts to
    None unconditionally. A non-NULL envelope with a NULL key_version is a
    pre-migration-031 plaintext row that was never backfilled — returned
    as-is (it IS the plaintext) rather than attempting to decrypt it."""
    if envelope is None:
        return None
    if key_version is None:
        return envelope
    kp = get_key_provider()
    return decrypt(envelope, kp.encryption_key(key_version), _aad(column, patient_id))


def compute_ssn_match_key(raw_ssn: Optional[str]) -> Optional[str]:
    """Normalize + blind-index an SSN for storage in patients.ssn_digits,
    always under the ACTIVE key version — every new write uses the current
    key, the same convention encrypt_patient_field follows. Returns None
    when there are no digits to index (no ssn supplied)."""
    normalized = normalize_ssn(raw_ssn)
    if normalized is None:
        return None
    kp = get_key_provider()
    return compute_blind_index(normalized, kp.blind_index_key(kp.active_key_version()))
