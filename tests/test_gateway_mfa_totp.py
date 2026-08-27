"""Unit tests for the TOTP wrapper (services/gateway/mfa_totp.py) — no DB,
no Redis, no HTTP. mfa_totp never logs anything, so there's nothing to
assert about logging here; the no-secrets-in-logs guarantee is exercised at
the route level (test_gateway_mfa_no_secrets_in_logs.py) where a secret
could actually reach a log call.
"""
import time

import pyotp
import pytest

from conftest import load_module

mfa_totp = load_module("services/gateway/mfa_totp.py", "gateway_mfa_totp")


def test_generate_secret_is_base32_and_nondeterministic():
    a = mfa_totp.generate_secret()
    b = mfa_totp.generate_secret()
    assert a != b
    assert len(a) >= 16
    pyotp.TOTP(a)  # does not raise — valid base32


def test_otpauth_uri_names_the_issuer_and_username():
    secret = mfa_totp.generate_secret()
    uri = mfa_totp.otpauth_uri(secret, "drnguyen")
    assert uri.startswith("otpauth://totp/")
    assert "drnguyen" in uri
    assert "Riverbend" in uri
    assert secret in uri  # this string is exactly what a QR code encodes


def test_verify_code_accepts_the_current_code():
    secret = mfa_totp.generate_secret()
    code = pyotp.TOTP(secret).now()
    step = mfa_totp.verify_code(secret, code)
    assert step is not None


def test_verify_code_rejects_a_wrong_code():
    secret = mfa_totp.generate_secret()
    assert mfa_totp.verify_code(secret, "000000") is None


def test_verify_code_rejects_empty_input():
    secret = mfa_totp.generate_secret()
    assert mfa_totp.verify_code(secret, "") is None
    assert mfa_totp.verify_code("", "123456") is None


def test_verify_code_tolerates_one_step_of_clock_drift():
    secret = mfa_totp.generate_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(time.time() // 30)
    previous_code = totp.generate_otp(current_step - 1)
    assert mfa_totp.verify_code(secret, previous_code) == current_step - 1


def test_verify_code_rejects_a_replayed_step():
    secret = mfa_totp.generate_secret()
    code = pyotp.TOTP(secret).now()
    step = mfa_totp.verify_code(secret, code)
    assert step is not None
    # The exact same code, presented again, must not verify a second time
    # once the caller records `step` as already accepted.
    assert mfa_totp.verify_code(secret, code, last_accepted_step=step) is None


def test_verify_code_replay_check_is_scoped_to_the_specific_step(monkeypatch):
    # A DIFFERENT valid step (e.g. clock drift tolerance) must still work
    # even if some other step was already accepted — replay prevention is
    # per-step, not "this secret can only ever be used once."
    secret = mfa_totp.generate_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(time.time() // 30)
    code_now = totp.generate_otp(current_step)
    code_prev = totp.generate_otp(current_step - 1)

    assert mfa_totp.verify_code(secret, code_prev, last_accepted_step=current_step) == current_step - 1
    assert mfa_totp.verify_code(secret, code_now, last_accepted_step=current_step - 1) == current_step
