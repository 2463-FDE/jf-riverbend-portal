"""One-time MFA recovery codes.

generate_codes() returns plaintext — the caller shows it to the user
exactly once and stores only hash_password()'s output (security.py's
existing PBKDF2-SHA256 hasher, the same format/iteration count used for
account passwords; a backup code is a credential too). Never log a
plaintext code or its hash's salt/iteration prefix alongside anything that
could be correlated back to the code.
"""
import secrets

_CODE_COUNT = 10
_CODE_LEN = 10
# Crockford-ish: no 0/O, 1/I/L, to cut down on a support call reading a code
# aloud over the phone. Uppercase only — codes are compared case-
# insensitively by the caller (app.py normalizes input before matching).
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def generate_codes(count: int = _CODE_COUNT, length: int = _CODE_LEN) -> list:
    """`count` fresh, cryptographically random plaintext codes. Uses
    secrets.choice (CSPRNG), not random — a backup code is a bearer
    credential to a clinical record."""
    return ["".join(secrets.choice(_ALPHABET) for _ in range(length)) for _ in range(count)]


def normalize(code: str) -> str:
    """Case/whitespace-insensitive comparison, matching how a person is
    likely to type a code read off a printed sheet."""
    return (code or "").strip().upper().replace("-", "").replace(" ", "")
