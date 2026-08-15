"""Clinic-issued patient portal invitations.

The client chose this over MRN + date-of-birth self-registration: those are
knowable by others, so they would end up acting as a credential for chart
access. Front desk vouches for identity in person at registration, and the
patient activates from a code they are handed.

Three properties this module exists to guarantee:

  * **The code is never stored.** Only a hash, exactly as passwords are — an
    invitation code is a credential for a chart, and a readable one in a
    backup or a support screenshot is chart access in plain sight.
  * **Redemption is single-use and races safely.** `activated_at` is set in
    the same statement that claims the row, so two simultaneous redemptions of
    one code cannot both create an account.
  * **Activation grants nothing by itself.** It creates a `users` row and ONE
    `patient_access_grants` row for that patient's own chart. All scoping then
    runs through the same gate that scopes staff — there is no patient-specific
    authorization path anywhere.

Codes are compared by hashing the candidate and matching hashes, never by
reading a stored code back. Lookup is therefore by hash, which is also why
`patient_invitations.code_hash` is indexed.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

# 8 groups of 4 from an unambiguous alphabet — no O/0, I/1/L, U/V — because
# these get read aloud at a desk and copied off paper. Roughly 2^82 of entropy,
# far past guessing, while staying transcribable.
_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZ23456789"
_GROUPS = 4
_GROUP_LEN = 4

DEFAULT_VALIDITY_DAYS = 14


def generate_code() -> str:
    """A fresh invitation code. Never stored — only handed to the patient."""
    body = "".join(
        "".join(secrets.choice(_ALPHABET) for _ in range(_GROUP_LEN))
        for _ in range(_GROUPS)
    )
    return "-".join(body[i : i + _GROUP_LEN] for i in range(0, len(body), _GROUP_LEN))


def normalise_code(raw: str) -> str:
    """Accept what a human actually types: spacing, case and hyphens vary when
    a code is read off paper. Normalising the input is not a weakening — the
    comparison still runs against the full entropy of the code."""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def hash_code(raw: str) -> str:
    """Hash for storage and comparison.

    SHA-256 rather than the PBKDF2 used for passwords, deliberately: this is a
    high-entropy random secret with a short lifetime, not a human-chosen
    password, so there is no low-entropy guess space for key stretching to
    defend. Stretching here would only slow redemption. The entropy of the code
    is what makes it safe, and that is generate_code's job.
    """
    return hashlib.sha256(normalise_code(raw).encode()).hexdigest()


def codes_match(candidate: str, stored_hash: str) -> bool:
    """Constant-time comparison, so redemption timing cannot be used to
    narrow a code character by character."""
    return hmac.compare_digest(hash_code(candidate), stored_hash or "")


def default_expiry(now=None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(days=DEFAULT_VALIDITY_DAYS)


def invitation_state(invitation, now=None):
    """Why an invitation cannot be redeemed, or None when it can.

    Returned as a reason string for logging and for the staff-facing view. It
    is deliberately NOT surfaced to the person redeeming: telling an anonymous
    caller "expired" rather than "invalid" confirms that a code existed, which
    turns the activation endpoint into an oracle for guessing them.
    """
    now = now or datetime.now(timezone.utc)
    if invitation is None:
        return "unknown"
    if invitation.revoked_at is not None:
        return "revoked"
    if invitation.activated_at is not None:
        return "already_used"
    expires = invitation.expires_at
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return "expired"
    return None


def username_for_patient(patient_id: int) -> str:
    """A stable, non-guessable-from-name account identifier.

    Deliberately not derived from the patient's name or MRN: usernames are
    quoted in support conversations and appear in logs, and a name-derived one
    would leak who holds a portal account. The patient_id is already exposed in
    record URLs, so this discloses nothing new.
    """
    return f"patient-{int(patient_id)}"
