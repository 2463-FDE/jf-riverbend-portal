"""TOTP second factor — production-readiness Stage 1 item 3.

Self-contained (RFC 6238 TOTP via pyotp): no SMS/email provider, so this
works the same in local dev and CI as anywhere else. app.py owns the
enrollment/challenge flow (login/login_mfa); this module only knows how to
mint a secret, build the otpauth:// URI a QR code encodes, and check a code.
"""
import pyotp


def generate_secret() -> str:
    return pyotp.random_base32()


def otpauth_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Riverbend Patient Portal")


def verify_code(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    # valid_window=1 tolerates one 30s step of clock drift either side,
    # matching how every real TOTP app/authenticator behaves.
    return pyotp.TOTP(secret).verify(code, valid_window=1)
