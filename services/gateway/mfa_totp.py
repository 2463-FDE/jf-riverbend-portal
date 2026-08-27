"""TOTP second factor (RFC 6238) via pyotp.

Self-contained: no SMS/email provider, so this behaves the same in local
dev, CI, and any real deployment. app.py owns the enrollment/challenge
routes; this module only knows how to mint a secret, build the otpauth://
URI a QR code encodes, and check a code against a decrypted secret —
callers own encryption (mfa_crypto.py) and storage.

Never log a secret, an otpauth:// URI (it embeds the secret), or a
submitted code — every function here takes them as plain arguments and
none of them call logging.
"""
import time
from typing import Optional

import pyotp

_ISSUER = "Riverbend Patient Portal"
# pyotp's own default (30s) — named explicitly here because verify_code's
# replay-prevention step arithmetic depends on it matching TOTP's interval.
_STEP_SECONDS = 30


def generate_secret() -> str:
    return pyotp.random_base32()


def otpauth_uri(secret: str, username: str) -> str:
    """The otpauth:// URI an authenticator app's QR scanner (or manual
    entry, for a device that can't scan) reads. Embeds the secret — never
    log or persist this string; regenerate it from the secret on demand."""
    return pyotp.TOTP(secret, interval=_STEP_SECONDS).provisioning_uri(name=username, issuer_name=_ISSUER)


def verify_code(secret: str, code: str, *, last_accepted_step: Optional[int] = None) -> Optional[int]:
    """Check `code` against `secret`, tolerating one 30s step of clock drift
    either side (matches how every real authenticator app behaves).

    Returns the matched time-step (an integer, safe to persist as
    users.mfa_last_totp_step) on success, or None on failure — including
    the case where the code is otherwise valid but its step exactly equals
    `last_accepted_step`, which means it was already used once and this is
    a replay within the same tolerance window.

    Deliberately does not use pyotp.TOTP.verify() directly: that returns a
    bare bool with no way to learn which step matched, and this module
    needs the step itself for replay prevention.
    """
    if not secret or not code:
        return None
    totp = pyotp.TOTP(secret, interval=_STEP_SECONDS)
    current_step = int(time.time() // _STEP_SECONDS)
    for step in (current_step - 1, current_step, current_step + 1):
        if step == last_accepted_step:
            continue
        if totp.generate_otp(step) == code:
            return step
    return None
