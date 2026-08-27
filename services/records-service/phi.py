"""records-service's own wiring onto libs/phi_crypto (adr/0012). Copy-pasted
from services/intake-service/phi.py, deliberately — see that file's
docstring and adr/0012 for why the primitives are shared but this wiring
isn't. records-service mostly DECRYPTS (it never writes ssn/dob/notes — see
models.py's "read-only here" column comments) with one exception: it OWNS
agent_draft_provenance.generated_text end to end (adr/0012 follow-up,
"agent draft text encryption") — created here (summary_agent_path.py via
agent_drafts.create_draft) and read back here (app.py's _draft_out) in the
same service, so unlike ssn/dob/notes this needs both directions.

get_key_provider() constructs EnvKeyProvider lazily, on first call, and
caches it — NOT at module import time. Importing this module (or app.py,
which the CI import-smoke-test and every unit test do) must not require
PHI_ACTIVE_KEY_VERSION/PHI_ENCRYPTION_KEY_V*/PHI_BLIND_INDEX_KEY_V* to be
set; only actually calling get_key_provider() does, and app.py's own
lifespan startup hook is what forces that call before this service accepts
any request — mirroring exactly how INTERNAL_SERVICE_TOKEN and
roles.yaml are already validated there, not at import time.
"""
from typing import Optional, Tuple

from libs.phi_crypto import EnvKeyProvider, KeyProvider, compute_blind_index, decrypt, encrypt

_key_provider: Optional[KeyProvider] = None


def get_key_provider() -> KeyProvider:
    global _key_provider
    if _key_provider is None:
        _key_provider = EnvKeyProvider()
    return _key_provider


def _aad(column: str, patient_id: int) -> bytes:
    return f"patients.{column}.{patient_id}".encode("utf-8")


def decrypt_patient_field(patient_id: int, column: str, envelope: Optional[str], key_version: Optional[str]) -> Optional[str]:
    """The read-side counterpart of intake-service's encrypt_patient_field
    — same AAD convention (patients.<column>.<patient_id>), required for
    decryption to succeed at all. A NULL envelope decrypts to None
    unconditionally. A non-NULL envelope with a NULL key_version is a
    pre-migration-031 plaintext row that was never backfilled — returned
    as-is (it IS the plaintext) rather than attempting to decrypt it."""
    if envelope is None:
        return None
    if key_version is None:
        return envelope
    kp = get_key_provider()
    return decrypt(envelope, kp.encryption_key(key_version), _aad(column, patient_id))


def _draft_aad(patient_id: int, version: int) -> bytes:
    # patient_id and version are BOTH frozen at insert by
    # agent_draft_provenance_guard_trigger (migration 020/032) — the same
    # "bind ciphertext to immutable identity" convention _aad() uses for
    # patients.<column>.<patient_id>, extended with `version` since a draft's
    # real identity is the (patient_id, version) pair, not patient_id alone.
    return f"agent_draft_provenance.generated_text.{patient_id}.{version}".encode("utf-8")


def encrypt_draft_text(patient_id: int, version: int, plaintext: str) -> Tuple[str, str]:
    """Always encrypts under the ACTIVE key version — every new draft is
    encrypted the instant it's created (adr/0012 follow-up), never left
    plaintext pending a later backfill. Returns (envelope, key_version);
    both are required (NOT NULL) on write, unlike patients.ssn/dob/notes
    which tolerate an absent value for not-yet-migrated rows."""
    kp = get_key_provider()
    version_id = kp.active_key_version()
    envelope = encrypt(plaintext, kp.encryption_key(version_id), _draft_aad(patient_id, version))
    return envelope, version_id


def decrypt_draft_text(patient_id: int, version: int, envelope: str, key_version: Optional[str]) -> str:
    """The read-side counterpart of encrypt_draft_text — same AAD
    convention, required for decryption to succeed. A NULL key_version
    means this row predates the backfill
    (db/migrations/scripts/encrypt_agent_draft_text.py) and `envelope` IS
    the plaintext — returned as-is, same "not yet migrated" contract as
    decrypt_patient_field above, for exactly the same reason (never guess
    at decrypting something that was never encrypted)."""
    if key_version is None:
        return envelope
    kp = get_key_provider()
    return decrypt(envelope, kp.encryption_key(key_version), _draft_aad(patient_id, version))


def compute_ssn_blind_index(normalized_ssn: Optional[str]) -> Optional[str]:
    """Blind-index an ALREADY-NORMALIZED ssn digit string, always under the
    ACTIVE key version — used by reconciliation.py's exact-match lookup,
    which normalizes with its own (stricter) validation before calling
    this. See libs/phi_crypto/blind_index.py: this does not normalize on
    the caller's behalf.

    Known limitation (not solved here, see adr/0012): a blind index is
    deterministic under a FIXED key. After a future key rotation, existing
    rows' ssn_digits stay computed under whatever key version was active
    when they were written, while a fresh lookup here always uses the
    then-current active version — so this search would silently stop
    matching rows written under a superseded version. No rotation has
    happened yet (every row is under one version), so this is a real, but
    currently inert, gap: solving it needs either a full ssn_digits
    re-index alongside any future rotation, or searching under every
    configured version, neither implemented in this change."""
    if not normalized_ssn:
        return None
    kp = get_key_provider()
    return compute_blind_index(normalized_ssn, kp.blind_index_key(kp.active_key_version()))
