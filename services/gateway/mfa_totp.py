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
    users.mfa_last_totp_step) on success, or None on failure.

    Round-1 review (M02): accepted steps must increase MONOTONICALLY, not
    merely avoid repeating the exact last one. The original check
    (`step == last_accepted_step: continue`) let a code from an OLDER step
    than the last accepted one through — e.g. accept step N, then step
    N-1 (still inside the +/-1 drift window) verifies successfully a
    second time, because N-1 was never itself marked used. That is a
    replay, just of a different code than the most recent one, not
    protection against replay. `step <= last_accepted_step` rejects every
    step at or behind the high-water mark, so a submitted code must be
    for a genuinely newer window than anything already accepted for this
    account. The accepted trade-off: a device whose clock corrects
    BACKWARD after a step has already been accepted gets a short window
    of rejected codes until real time catches back up past the
    high-water mark — safer than accepting a replay, and self-resolving
    within a step or two without any support action.

    Deliberately does not use pyotp.TOTP.verify() directly: that returns a
    bare bool with no way to learn which step matched, and this module
    needs the step itself for replay prevention. Callers must additionally
    persist the returned step via an atomic compare-and-set (see
    app.py::_claim_totp_step) — this function alone only protects against
    sequential reuse within one caller's own view of last_accepted_step,
    not two concurrent requests racing to claim the same step.
    """
    if not secret or not code:
        return None
    totp = pyotp.TOTP(secret, interval=_STEP_SECONDS)
    current_step = int(time.time() // _STEP_SECONDS)
    for step in (current_step - 1, current_step, current_step + 1):
        if last_accepted_step is not None and step <= last_accepted_step:
            continue
        if totp.generate_otp(step) == code:
            return step
    return None
