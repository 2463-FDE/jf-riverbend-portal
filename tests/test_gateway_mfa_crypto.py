"""Unit tests for services/gateway/mfa_crypto.py — the MFA-specific AEAD key
provider and encrypt/decrypt helpers. Deliberately parallels
tests/test_phi_crypto.py's key-provider coverage, but against MFA_* env vars,
never PHI_* — the whole point of this module is that the two are independent.
"""
import base64
import os

import pytest

from conftest import load_module

mfa_crypto = load_module("services/gateway/mfa_crypto.py", "gateway_mfa_crypto")


def _b64_key(byte: int = 1) -> str:
    return base64.b64encode(bytes([byte]) * 32).decode()


def _env(**overrides) -> dict:
    base = {
        "MFA_ACTIVE_KEY_VERSION": "v1",
        "MFA_ENCRYPTION_KEY_V1": _b64_key(1),
    }
    base.update(overrides)
    return base


# --- fail-closed key configuration ------------------------------------------


def test_missing_active_version_fails_closed():
    with pytest.raises(mfa_crypto.MfaKeyConfigurationError):
        mfa_crypto.MfaEnvKeyProvider(env={})


def test_active_version_with_no_matching_key_fails_closed():
    with pytest.raises(mfa_crypto.MfaKeyConfigurationError):
        mfa_crypto.MfaEnvKeyProvider(env={"MFA_ACTIVE_KEY_VERSION": "v2", "MFA_ENCRYPTION_KEY_V1": _b64_key()})


def test_malformed_base64_fails_closed():
    with pytest.raises(mfa_crypto.MfaKeyConfigurationError):
        mfa_crypto.MfaEnvKeyProvider(env={"MFA_ACTIVE_KEY_VERSION": "v1", "MFA_ENCRYPTION_KEY_V1": "not-base64!"})


def test_wrong_length_key_fails_closed():
    short = base64.b64encode(b"too-short").decode()
    with pytest.raises(mfa_crypto.MfaKeyConfigurationError):
        mfa_crypto.MfaEnvKeyProvider(env={"MFA_ACTIVE_KEY_VERSION": "v1", "MFA_ENCRYPTION_KEY_V1": short})


def test_a_malformed_predecessor_key_fails_construction_even_though_unused():
    # Kept around for rotation but never referenced as active — must still
    # be validated eagerly, not lazily the first time something needs it.
    env = _env(MFA_ENCRYPTION_KEY_V0="not-base64!")
    with pytest.raises(mfa_crypto.MfaKeyConfigurationError):
        mfa_crypto.MfaEnvKeyProvider(env=env)


def test_unknown_key_version_on_read_fails_closed():
    kp = mfa_crypto.MfaEnvKeyProvider(env=_env())
    with pytest.raises(mfa_crypto.MfaUnknownKeyVersionError):
        kp.encryption_key("v99")


def test_get_key_provider_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("MFA_ACTIVE_KEY_VERSION", raising=False)
    mfa_crypto.reset_key_provider()
    with pytest.raises(mfa_crypto.MfaKeyConfigurationError):
        mfa_crypto.get_key_provider()
    mfa_crypto.reset_key_provider()


# --- encrypt/decrypt roundtrip + AEAD authentication ------------------------


@pytest.fixture
def key_provider(monkeypatch):
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    mfa_crypto.reset_key_provider()
    yield
    mfa_crypto.reset_key_provider()


def test_roundtrip(key_provider):
    envelope, version = mfa_crypto.encrypt_totp_secret(42, "JBSWY3DPEHPK3PXP")
    assert version == "v1"
    assert "JBSWY3DPEHPK3PXP" not in envelope  # ciphertext, not plaintext
    assert mfa_crypto.decrypt_totp_secret(42, envelope, version) == "JBSWY3DPEHPK3PXP"


def test_encryption_is_nondeterministic(key_provider):
    a, _ = mfa_crypto.encrypt_totp_secret(42, "SAMESECRET")
    b, _ = mfa_crypto.encrypt_totp_secret(42, "SAMESECRET")
    assert a != b  # fresh random nonce per call


def test_decrypt_fails_under_a_different_users_aad(key_provider):
    envelope, version = mfa_crypto.encrypt_totp_secret(42, "JBSWY3DPEHPK3PXP")
    with pytest.raises(Exception):
        mfa_crypto.decrypt_totp_secret(999, envelope, version)  # wrong user_id -> wrong AAD


def test_tampered_ciphertext_fails_to_decrypt(key_provider):
    envelope, version = mfa_crypto.encrypt_totp_secret(42, "JBSWY3DPEHPK3PXP")
    tampered = envelope[:-4] + ("A" if envelope[-4] != "A" else "B") + envelope[-3:]
    with pytest.raises(Exception):
        mfa_crypto.decrypt_totp_secret(42, tampered, version)


def test_decrypt_under_a_key_version_this_process_does_not_have_fails_closed(key_provider):
    envelope, _ = mfa_crypto.encrypt_totp_secret(42, "JBSWY3DPEHPK3PXP")
    with pytest.raises(mfa_crypto.MfaUnknownKeyVersionError):
        mfa_crypto.decrypt_totp_secret(42, envelope, "v99")


def test_mfa_keys_are_independent_of_phi_keys(monkeypatch):
    # The whole point of this module: PHI_* env vars configured (or not)
    # must have zero bearing on MFA key resolution.
    monkeypatch.delenv("PHI_ACTIVE_KEY_VERSION", raising=False)
    monkeypatch.delenv("PHI_ENCRYPTION_KEY_V1", raising=False)
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    mfa_crypto.reset_key_provider()
    envelope, version = mfa_crypto.encrypt_totp_secret(1, "SECRET")
    assert mfa_crypto.decrypt_totp_secret(1, envelope, version) == "SECRET"
    mfa_crypto.reset_key_provider()
