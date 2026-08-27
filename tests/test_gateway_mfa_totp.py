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


# --- Round-1 review (M02): accepted steps must increase MONOTONICALLY ------
#
# The original design only rejected an EXACT repeat of last_accepted_step,
# which let an older step still inside the +/-1 drift window verify
# successfully after a newer one had already been accepted — a replay, just
# of a different code. `step <= last_accepted_step` closes that: once step N
# has been accepted, nothing at or behind N verifies again, ever, regardless
# of whether N itself is being repeated or a neighboring step is being tried
# instead.


def test_last_accepted_is_current_step_previous_step_is_rejected():
    secret = mfa_totp.generate_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(time.time() // 30)
    previous_code = totp.generate_otp(current_step - 1)

    assert mfa_totp.verify_code(secret, previous_code, last_accepted_step=current_step) is None


def test_last_accepted_is_current_step_current_step_is_rejected():
    secret = mfa_totp.generate_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(time.time() // 30)
    current_code = totp.generate_otp(current_step)

    assert mfa_totp.verify_code(secret, current_code, last_accepted_step=current_step) is None


def test_last_accepted_is_current_step_next_step_is_accepted_within_drift_window():
    # The +1 branch of the drift-tolerance window is the only one that can
    # ever be genuinely newer than an already-accepted current step.
    secret = mfa_totp.generate_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(time.time() // 30)
    next_code = totp.generate_otp(current_step + 1)

    assert mfa_totp.verify_code(secret, next_code, last_accepted_step=current_step) == current_step + 1


def test_last_accepted_is_previous_step_current_step_is_accepted():
    secret = mfa_totp.generate_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(time.time() // 30)
    current_code = totp.generate_otp(current_step)

    assert mfa_totp.verify_code(secret, current_code, last_accepted_step=current_step - 1) == current_step


def test_last_accepted_is_previous_step_that_same_previous_step_is_rejected():
    secret = mfa_totp.generate_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(time.time() // 30)
    previous_code = totp.generate_otp(current_step - 1)

    assert mfa_totp.verify_code(secret, previous_code, last_accepted_step=current_step - 1) is None


def test_no_last_accepted_step_still_supports_the_full_drift_window():
    # Unchanged behavior for a brand-new enrollment (mfa_last_totp_step is
    # NULL) — both the previous and current steps inside the tolerance
    # window verify when there is nothing yet to be monotonic against.
    secret = mfa_totp.generate_secret()
    totp = pyotp.TOTP(secret)
    current_step = int(time.time() // 30)

    assert mfa_totp.verify_code(secret, totp.generate_otp(current_step - 1)) == current_step - 1
    assert mfa_totp.verify_code(secret, totp.generate_otp(current_step)) == current_step
