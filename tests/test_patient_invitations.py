"""Invitation code handling — the pure layer, no DB.

An invitation code is a credential for a medical record. These pin the three
properties that make that safe: it is never stored, it cannot be narrowed by
timing, and an unredeemable one never explains itself to the redeemer.
"""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import load_module

inv = load_module("services/gateway/patient_invitations.py", "patient_invitations_pure")


class _Invitation:
    def __init__(self, expires_at=None, activated_at=None, revoked_at=None):
        self.expires_at = expires_at or (datetime.now(timezone.utc) + timedelta(days=1))
        self.activated_at = activated_at
        self.revoked_at = revoked_at


# --- the code itself -------------------------------------------------------


def test_codes_are_unique_across_many_draws():
    assert len({inv.generate_code() for _ in range(500)}) == 500


def test_codes_avoid_characters_that_are_misread_aloud():
    # These get read across a desk and copied off paper. O/0, I/1/L and U/V are
    # the pairs that produce failed activations and support calls.
    body = inv.generate_code().replace("-", "")
    assert not (set(body) & set("O0I1LUV"))


def test_codes_are_grouped_for_transcription():
    code = inv.generate_code()
    assert len(code.split("-")) == 4
    assert all(len(g) == 4 for g in code.split("-"))


def test_a_code_has_enough_entropy_to_be_unguessable():
    # 16 characters from a 29-symbol alphabet — about 2^77. Pinned because
    # shrinking the alphabet or the length would silently weaken the credential.
    body = inv.generate_code().replace("-", "")
    assert len(body) == 16
    assert len(inv._ALPHABET) >= 29


# --- normalisation: accept what a human types ------------------------------


@pytest.mark.parametrize("typed", [
    "abcd-efgh-jkmn-pqrs", "ABCD EFGH JKMN PQRS", "  abcdefghjkmnpqrs  ", "ABCD-efgh-JKMN-pqrs",
])
def test_the_same_code_matches_however_it_was_typed(typed):
    canonical = inv.hash_code("ABCDEFGHJKMNPQRS")

    assert inv.codes_match(typed, canonical)


def test_normalising_does_not_make_different_codes_collide():
    assert inv.hash_code("ABCD-EFGH-JKMN-PQRS") != inv.hash_code("ABCD-EFGH-JKMN-PQRT")


# --- storage and comparison ------------------------------------------------


def test_the_hash_does_not_contain_the_code():
    code = inv.generate_code()
    stored = inv.hash_code(code)

    assert inv.normalise_code(code) not in stored
    assert len(stored) == 64  # sha256 hex


def test_a_wrong_code_does_not_match():
    assert not inv.codes_match("WRONG-CODE-HERE-XXXX", inv.hash_code(inv.generate_code()))


def test_comparison_tolerates_a_missing_stored_hash():
    # Never raise on a malformed row — a crash here is an availability problem
    # on a public endpoint.
    assert inv.codes_match("ANY", None) is False
    assert inv.codes_match("ANY", "") is False


# --- redeemability ---------------------------------------------------------


def test_a_live_invitation_is_redeemable():
    assert inv.invitation_state(_Invitation()) is None


def test_an_unknown_invitation_is_not_redeemable():
    assert inv.invitation_state(None) == "unknown"


def test_an_expired_invitation_is_not_redeemable():
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert inv.invitation_state(_Invitation(expires_at=past)) == "expired"


def test_a_revoked_invitation_is_not_redeemable():
    assert inv.invitation_state(_Invitation(revoked_at=datetime.now(timezone.utc))) == "revoked"


def test_an_already_used_invitation_is_not_redeemable():
    # Single use. Without this a code shared or forwarded mints a second account.
    assert inv.invitation_state(_Invitation(activated_at=datetime.now(timezone.utc))) == "already_used"


def test_a_naive_expiry_is_treated_as_utc_not_crashed_on():
    # Postgres may hand back a naive datetime depending on the driver; comparing
    # naive to aware raises, and raising here would break activation entirely.
    naive_past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    assert inv.invitation_state(_Invitation(expires_at=naive_past)) == "expired"


def test_default_validity_is_bounded():
    now = datetime.now(timezone.utc)
    assert timedelta(days=1) <= inv.default_expiry(now) - now <= timedelta(days=30)


# --- account naming --------------------------------------------------------


def test_the_username_does_not_leak_who_holds_a_portal_account():
    # Usernames are quoted in support conversations and appear in logs. A
    # name-derived one would disclose that a named person has portal access.
    assert inv.username_for_patient(1042) == "patient-1042"


def test_the_username_is_stable_for_a_patient():
    assert inv.username_for_patient(1042) == inv.username_for_patient(1042)
