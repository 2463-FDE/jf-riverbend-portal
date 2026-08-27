"""Codex review (2026-08-07, PR #22 — medium) — services/records-service/
reconciliation.py::_normalize_ssn used to treat ANY non-empty digit string
as a valid match key, so a placeholder like "000-00-0000" or a partial/
mistyped SSN grouped unrelated patients as an "exact match," producing false
cross-chart allergy/medication discrepancy evidence a clinician could act on.

Direct unit tests of the validation logic (not the full HTTP route) since
this is pure function behavior — services/records-service/app.py::
get_patient_reconciliation and tests/test_records_reconciliation_route.py
exercise the end-to-end route/authorization path.
"""
import base64
import os
import re
import sys

import pytest

from conftest import load_module

reconciliation = load_module("services/records-service/reconciliation.py", "reconciliation_ssn_validation")

_normalize_ssn = reconciliation._normalize_ssn

# w8-planner-2 P2 (adr/0012): find_ssn_match_ids now blind-indexes the query
# key (compute_ssn_blind_index) instead of comparing raw digits — needs a
# configured PHI key provider. reconciliation.py's `from phi import ...`
# resolves through sys.modules, so patching that module (not reconciliation
# itself) is what actually affects it — see test_intake_healthz.py's
# identical note.
phi_mod = sys.modules["phi"]
_TEST_PHI_PROVIDER = phi_mod.EnvKeyProvider(
    {
        "PHI_ACTIVE_KEY_VERSION": "v1",
        "PHI_ENCRYPTION_KEY_V1": base64.b64encode(os.urandom(32)).decode(),
        "PHI_BLIND_INDEX_KEY_V1": base64.b64encode(os.urandom(32)).decode(),
    }
)


@pytest.fixture(autouse=True)
def _configured_phi_key_provider(monkeypatch):
    monkeypatch.setattr(phi_mod, "_key_provider", _TEST_PHI_PROVIDER)


class _FakePatient:
    def __init__(self, id, ssn):
        self.id = id
        # w8-planner-2 P2 (adr/0012): migration 031 replaced the raw-digit
        # generated column with an HMAC-SHA256 blind index — mirror that
        # here (still pure digit extraction first, no SSA-invalid-pattern
        # validation; that stays in _normalize_ssn, applied to the query
        # key — see find_ssn_match_ids) so a fake row "matches" a query the
        # same way a real blind-indexed column would.
        digits = re.sub(r"\D", "", ssn) if ssn else None
        self.ssn_digits = reconciliation.compute_ssn_blind_index(digits) if digits else None


class _FakeExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, patients):
        self._patients = patients

    def execute(self, _stmt):
        return _FakeExecResult(self._patients)


# --- _normalize_ssn: valid inputs still work --------------------------------


def test_valid_ssn_with_dashes_normalizes_to_digits():
    assert _normalize_ssn("412-55-9981") == "412559981"


def test_valid_ssn_already_digits_only_normalizes_unchanged():
    assert _normalize_ssn("412559981") == "412559981"


# --- _normalize_ssn: the review's own examples and the standard SSA-invalid
# patterns are all rejected -------------------------------------------------


def test_all_zero_ssn_is_rejected():
    assert _normalize_ssn("000-00-0000") is None


def test_none_and_empty_string_are_rejected():
    assert _normalize_ssn(None) is None
    assert _normalize_ssn("") is None


def test_short_or_partial_ssn_is_rejected():
    assert _normalize_ssn("123-45") is None
    assert _normalize_ssn("1234") is None


def test_too_long_ssn_is_rejected():
    assert _normalize_ssn("1234567890") is None


def test_all_same_digit_repeated_is_rejected():
    for digit in "0123456789":
        assert _normalize_ssn(digit * 9) is None, f"{digit * 9} should be rejected"


def test_area_000_is_rejected():
    assert _normalize_ssn("000-12-3456") is None


def test_area_666_is_rejected():
    assert _normalize_ssn("666-12-3456") is None


def test_area_900_to_999_is_rejected():
    assert _normalize_ssn("900-12-3456") is None
    assert _normalize_ssn("999-12-3456") is None


def test_group_00_is_rejected():
    assert _normalize_ssn("123-00-4567") is None


def test_serial_0000_is_rejected():
    assert _normalize_ssn("123-45-0000") is None


def test_valid_boundary_areas_are_still_accepted():
    # 001 and 899 are real, issuable areas — the fix must not overreach into
    # rejecting valid SSNs near the invalid boundaries.
    assert _normalize_ssn("001-23-4567") == "001234567"
    assert _normalize_ssn("899-23-4567") == "899234567"


# --- find_ssn_match_ids: an invalid-shaped SSN produces zero candidates,
# from either the requested patient's own SSN or a candidate row's ---------


def test_find_ssn_match_ids_returns_nothing_for_a_placeholder_requested_ssn():
    db = _FakeDb([_FakePatient(2, "000-00-0000")])
    assert reconciliation.find_ssn_match_ids(db, patient_id=1, ssn="000-00-0000") == []


def test_find_ssn_match_ids_excludes_a_candidate_whose_stored_ssn_is_invalid():
    # Requested patient has a genuinely valid SSN; a candidate row happens
    # to have an invalid one on file. That candidate must never match
    # anything (it can't even match itself), regardless of what raw string
    # is on either side.
    db = _FakeDb([_FakePatient(2, "000-00-0000"), _FakePatient(3, "412-55-9981")])
    matches = reconciliation.find_ssn_match_ids(db, patient_id=1, ssn="412-55-9981")
    assert matches == [3]
