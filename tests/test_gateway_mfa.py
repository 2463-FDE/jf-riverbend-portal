"""Unit tests for services/gateway/mfa.py — pure TOTP wrapper, no DB/Redis."""
import time

import pyotp

from conftest import load_module

mfa = load_module("services/gateway/mfa.py", "gateway_mfa")


def test_generate_secret_is_a_valid_base32_totp_secret():
    secret = mfa.generate_secret()
    assert secret
    # Must actually work as a TOTP secret, not just look like base32.
    assert pyotp.TOTP(secret).now().isdigit()


def test_otpauth_uri_carries_the_secret_username_and_issuer():
    secret = mfa.generate_secret()
    uri = mfa.otpauth_uri(secret, "frontdesk")

    assert uri.startswith("otpauth://totp/")
    assert "secret=" + secret in uri
    assert "Riverbend" in uri
    assert "frontdesk" in uri


def test_verify_code_accepts_the_current_code():
    secret = mfa.generate_secret()
    code = pyotp.TOTP(secret).now()

    assert mfa.verify_code(secret, code) is True


def test_verify_code_rejects_a_wrong_code():
    secret = mfa.generate_secret()
    wrong = str((int(pyotp.TOTP(secret).now()) + 1) % 1_000_000).zfill(6)

    assert mfa.verify_code(secret, wrong) is False


def test_verify_code_rejects_empty_or_missing_input():
    secret = mfa.generate_secret()

    assert mfa.verify_code(secret, "") is False
    assert mfa.verify_code("", "123456") is False
    assert mfa.verify_code("", "") is False


def test_verify_code_tolerates_one_step_of_clock_drift():
    secret = mfa.generate_secret()
    totp = pyotp.TOTP(secret)
    one_step_ago = totp.at(time.time() - 30)

    assert mfa.verify_code(secret, one_step_ago) is True
