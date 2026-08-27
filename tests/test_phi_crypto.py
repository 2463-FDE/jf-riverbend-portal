"""libs/phi_crypto — AEAD field encryption, blind indexing, and key
management (w8-planner-2 P2, adr/0012).

Two kinds of coverage here: correctness (round-trip, determinism where
intended, non-determinism where intended) and failure-closed behavior (every
malformed/missing/identical key combination EnvKeyProvider is specified to
reject — adr/0012's exact startup-test list).
"""
import base64
import os

import pytest

from libs.phi_crypto import (
    DecryptionError,
    EnvelopeFormatError,
    EnvKeyProvider,
    KeyConfigurationError,
    UnknownKeyVersionError,
    compute_blind_index,
    decrypt,
    encrypt,
    normalize_ssn,
)


def _b64_key(raw: bytes | None = None) -> str:
    return base64.b64encode(raw if raw is not None else os.urandom(32)).decode()


def _env(active="v1", enc=None, idx=None, **extra):
    env = {
        "PHI_ACTIVE_KEY_VERSION": active,
        "PHI_ENCRYPTION_KEY_V1": enc if enc is not None else _b64_key(),
        "PHI_BLIND_INDEX_KEY_V1": idx if idx is not None else _b64_key(),
    }
    env.update(extra)
    return env


# --- encrypt/decrypt: round trip, AAD binding, randomization -----------------


def test_round_trip_recovers_the_original_plaintext():
    key = os.urandom(32)
    envelope = encrypt("412-55-9981", key, b"patients.ssn.1042")
    assert decrypt(envelope, key, b"patients.ssn.1042") == "412-55-9981"


def test_envelope_is_not_the_plaintext():
    key = os.urandom(32)
    envelope = encrypt("412-55-9981", key, b"patients.ssn.1042")
    assert "412-55-9981" not in envelope


def test_the_same_plaintext_encrypted_twice_produces_different_ciphertext():
    key = os.urandom(32)
    e1 = encrypt("412-55-9981", key, b"patients.ssn.1042")
    e2 = encrypt("412-55-9981", key, b"patients.ssn.1042")
    assert e1 != e2
    # both still decrypt correctly — randomization is in the nonce, not a bug
    assert decrypt(e1, key, b"patients.ssn.1042") == "412-55-9981"
    assert decrypt(e2, key, b"patients.ssn.1042") == "412-55-9981"


def test_decrypting_with_the_wrong_key_fails_closed():
    envelope = encrypt("412-55-9981", os.urandom(32), b"patients.ssn.1042")
    with pytest.raises(DecryptionError):
        decrypt(envelope, os.urandom(32), b"patients.ssn.1042")


def test_decrypting_with_the_wrong_aad_fails_closed():
    # Proves row/column binding: ciphertext for patient 1042's ssn must not
    # decrypt as if it belonged to patient 9999, or to the dob column.
    key = os.urandom(32)
    envelope = encrypt("412-55-9981", key, b"patients.ssn.1042")
    with pytest.raises(DecryptionError):
        decrypt(envelope, key, b"patients.ssn.9999")
    with pytest.raises(DecryptionError):
        decrypt(envelope, key, b"patients.dob.1042")


def test_tampered_ciphertext_fails_closed():
    key = os.urandom(32)
    envelope = encrypt("412-55-9981", key, b"patients.ssn.1042")
    raw = bytearray(base64.b64decode(envelope))
    raw[-1] ^= 0xFF  # flip a bit in the AEAD tag/ciphertext
    tampered = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(DecryptionError):
        decrypt(tampered, key, b"patients.ssn.1042")


def test_malformed_envelope_is_a_format_error_not_a_decryption_error():
    key = os.urandom(32)
    with pytest.raises(EnvelopeFormatError):
        decrypt("not valid base64 at all!!!", key, b"patients.ssn.1042")


def test_wrong_key_length_is_a_format_error():
    with pytest.raises(EnvelopeFormatError):
        encrypt("412-55-9981", os.urandom(16), b"patients.ssn.1042")


# --- blind index: deterministic, keyed, not reversible without the key ------


def test_blind_index_is_deterministic_for_the_same_input_and_key():
    key = os.urandom(32)
    assert compute_blind_index("412559981", key) == compute_blind_index("412559981", key)


def test_blind_index_differs_across_keys():
    a = compute_blind_index("412559981", os.urandom(32))
    b = compute_blind_index("412559981", os.urandom(32))
    assert a != b


def test_blind_index_differs_across_inputs():
    key = os.urandom(32)
    assert compute_blind_index("412559981", key) != compute_blind_index("412559982", key)


def test_normalize_ssn_strips_formatting_to_bare_digits():
    assert normalize_ssn("412-55-9981") == "412559981"
    assert normalize_ssn("412 55 9981") == "412559981"
    assert normalize_ssn("412559981") == "412559981"


def test_normalize_ssn_returns_none_for_blank_or_missing():
    assert normalize_ssn(None) is None
    assert normalize_ssn("") is None


# --- EnvKeyProvider: the exact adr/0012 startup-test list -------------------


def test_valid_independent_keys_succeed():
    kp = EnvKeyProvider(_env())
    assert kp.active_key_version() == "v1"
    assert len(kp.encryption_key("v1")) == 32
    assert len(kp.blind_index_key("v1")) == 32
    assert kp.encryption_key("v1") != kp.blind_index_key("v1")


def test_missing_active_key_version_fails_startup():
    env = _env()
    del env["PHI_ACTIVE_KEY_VERSION"]
    with pytest.raises(KeyConfigurationError):
        EnvKeyProvider(env)


def test_missing_encryption_key_fails_startup():
    env = _env()
    del env["PHI_ENCRYPTION_KEY_V1"]
    with pytest.raises(KeyConfigurationError):
        EnvKeyProvider(env)


def test_missing_blind_index_key_fails_startup():
    env = _env()
    del env["PHI_BLIND_INDEX_KEY_V1"]
    with pytest.raises(KeyConfigurationError):
        EnvKeyProvider(env)


def test_malformed_base64_fails_startup():
    with pytest.raises(KeyConfigurationError):
        EnvKeyProvider(_env(enc="not-valid-base64!!!"))


def test_decoded_key_not_exactly_32_bytes_fails_startup():
    with pytest.raises(KeyConfigurationError):
        EnvKeyProvider(_env(enc=_b64_key(os.urandom(16))))
    with pytest.raises(KeyConfigurationError):
        EnvKeyProvider(_env(idx=_b64_key(os.urandom(48))))


def test_identical_encryption_and_blind_index_keys_fail_startup():
    same = _b64_key()
    with pytest.raises(KeyConfigurationError):
        EnvKeyProvider(_env(enc=same, idx=same))


def test_error_messages_name_the_variable_but_never_the_value():
    secret_value = "not-valid-base64!!!"
    try:
        EnvKeyProvider(_env(enc=secret_value))
        raise AssertionError("expected KeyConfigurationError")
    except KeyConfigurationError as e:
        assert "PHI_ENCRYPTION_KEY_V1" in str(e)
        assert secret_value not in str(e)


# --- key rotation: a superseded version stays decryptable -------------------


def test_a_previous_key_version_configured_alongside_the_active_one_still_decrypts():
    v0_enc, v0_idx = os.urandom(32), os.urandom(32)
    v1_enc, v1_idx = os.urandom(32), os.urandom(32)
    kp = EnvKeyProvider(
        {
            "PHI_ACTIVE_KEY_VERSION": "v1",
            "PHI_ENCRYPTION_KEY_V1": _b64_key(v1_enc),
            "PHI_BLIND_INDEX_KEY_V1": _b64_key(v1_idx),
            "PHI_ENCRYPTION_KEY_V0": _b64_key(v0_enc),
            "PHI_BLIND_INDEX_KEY_V0": _b64_key(v0_idx),
        }
    )
    old_envelope = encrypt("412-55-9981", v0_enc, b"patients.ssn.1042")
    assert decrypt(old_envelope, kp.encryption_key("v0"), b"patients.ssn.1042") == "412-55-9981"
    assert kp.active_key_version() == "v1"


def test_an_unconfigured_key_version_is_refused_not_silently_substituted():
    kp = EnvKeyProvider(_env())
    with pytest.raises(UnknownKeyVersionError):
        kp.encryption_key("v9")
    with pytest.raises(UnknownKeyVersionError):
        kp.blind_index_key("v9")
