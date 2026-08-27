"""Unit tests for services/gateway/mfa_backup_codes.py."""
from conftest import load_module

mfa_backup_codes = load_module("services/gateway/mfa_backup_codes.py", "gateway_mfa_backup_codes")


def test_generates_ten_codes_by_default():
    codes = mfa_backup_codes.generate_codes()
    assert len(codes) == 10


def test_codes_are_unique_within_a_batch():
    codes = mfa_backup_codes.generate_codes()
    assert len(set(codes)) == len(codes)


def test_codes_exclude_ambiguous_characters():
    codes = mfa_backup_codes.generate_codes()
    for code in codes:
        assert not set(code) & set("0O1IL"), code


def test_normalize_is_case_and_whitespace_insensitive():
    assert mfa_backup_codes.normalize(" ab-cd 12 ") == "ABCD12"
    assert mfa_backup_codes.normalize("abcd12") == mfa_backup_codes.normalize("ABCD12")


def test_normalize_handles_empty_input():
    assert mfa_backup_codes.normalize("") == ""
    assert mfa_backup_codes.normalize(None) == ""
