"""Safe-Harbor scrub — W8.

Two kinds of test here, and the second kind matters more.

The first asserts each detectable identifier category is removed. The second
asserts the LIMITS: a third-party name in prose survives, a bare year survives,
an age of 89 survives. Those are not gaps in the tests — they are the residual
risk this control cannot close, pinned so nobody later reads a green suite as
"the data is de-identified". It is not. See safe_harbor.py's docstring.
"""
import pytest

from libs.deid import (
    IDENTIFIER_CATEGORIES,
    field_category,
    RESIDUAL_RISK_CATEGORIES,
    scrub,
    scrub_structured,
)

MASK = "[REDACTED]"


def _cats(report):
    return {k.split(":", 1)[0] for k in report.counts}


# --- the eighteen categories, by letter ------------------------------------ #


def test_all_eighteen_categories_are_enumerated():
    # Coverage is checked against the rule, not against this file's imagination.
    assert set(IDENTIFIER_CATEGORIES) == set("ABCDEFGHIJKLMNOPQR")
    assert len(IDENTIFIER_CATEGORIES) == 18


@pytest.mark.parametrize("text,category", [
    ("ssn 123-45-6789", "G"),
    ("reach me at bob@example.org", "F"),
    ("see https://portal.example.org/x", "N"),
    ("from 10.1.2.3", "O"),
    ("fax: 555-222-3333", "E"),
    ("call 555-123-4567", "D"),
    ("MRN: AB1234", "H"),
    ("member #XYZ12345", "I"),
    ("acct: 99881", "J"),
    ("license: DL8899", "K"),
    ("VIN: 1HGCM8266", "L"),
    ("serial: SN44551", "M"),
    ("zip 90210", "B"),
    ("admitted 2026-03-02", "C"),
    ("admitted 3/2/2026", "C"),
])
def test_each_detectable_category_is_removed(text, category):
    out, report = scrub(text)

    assert category in _cats(report), f"category {category} did not fire"
    assert out != text


def test_a_known_name_is_removed_as_one_unit():
    # Longest-first ordering: "Maria Gonzalez" must not leave "Gonzalez" behind.
    out, report = scrub("Maria Gonzalez arrived", known_identifiers=["Gonzalez", "Maria Gonzalez"])

    assert "Maria" not in out and "Gonzalez" not in out
    assert "A" in _cats(report)


def test_an_age_over_89_is_aggregated_and_89_is_not():
    over, _ = scrub("patient is 94 years old")
    under, report = scrub("patient is 89 years old")

    assert "90+" in over
    assert "89 years old" in under, "Safe Harbor permits ages up to and including 89"
    assert not report


def test_a_bare_year_survives():
    # Safe Harbor permits the year. Masking it would destroy clinical utility
    # for no privacy gain.
    out, report = scrub("first diagnosed in 2019")

    assert "2019" in out
    assert not report


# --- the limits, asserted on purpose --------------------------------------- #


def test_a_third_party_name_in_prose_is_NOT_removed():
    """The honest limit. A pattern cannot find a name it was not given, so a
    relative mentioned in narrative survives. This is why the scrub alone does
    not achieve Safe Harbor."""
    out, _ = scrub("her daughter Ana drove her", known_identifiers=["Maria Gonzalez"])

    assert "Ana" in out


def test_residual_risk_is_declared_as_data():
    # The recommendation gate enumerates these rather than restating them, so
    # they have to be machine-readable and to name real categories.
    assert set(RESIDUAL_RISK_CATEGORIES) <= set(IDENTIFIER_CATEGORIES)
    assert {"A", "P", "Q", "R"} <= set(RESIDUAL_RISK_CATEGORIES)


def test_the_report_never_contains_what_it_removed():
    """A de-identification log holding the identifiers recreates the exposure."""
    secret = "123-45-6789"
    _, report = scrub(f"ssn {secret}")

    assert secret not in report.summary()
    assert all(secret not in str(k) for k in report.counts)
    assert report.total == 1


# --- structured payloads --------------------------------------------------- #


def test_sensitive_keys_are_masked_and_free_text_values_scrubbed():
    payload = {
        "patient_id": 1042,
        "ssn": "123-45-6789",
        "notes": "seen 2026-03-02, call 555-123-4567",
        "nested": [{"email": "x@y.org"}, "MRN: AB1234"],
    }

    out, report = scrub_structured(payload)

    assert out["ssn"] == MASK
    assert out["notes"] == MASK          # key is sensitive, so the value goes entirely
    assert out["patient_id"] == 1042     # not an identifier under Safe Harbor on its own
    assert out["nested"][0]["email"] == MASK
    assert MASK in out["nested"][1]      # free-text MRN inside a list
    assert report.total >= 3


def test_structured_scrub_does_not_mutate_the_input():
    payload = {"ssn": "123-45-6789"}
    scrub_structured(payload)

    assert payload["ssn"] == "123-45-6789"


def test_empty_and_none_are_handled():
    assert scrub("")[0] == ""
    assert scrub(None)[0] is None
    out, _ = scrub_structured({"a": None, "b": 3})
    assert out == {"a": None, "b": 3}


def test_the_field_name_list_is_shared_not_duplicated():
    """Two lists of PHI field names drift, and the one that drifts is the one
    nobody is looking at."""
    from libs.deid import safe_harbor
    from libs.safe_logging.redact import SENSITIVE_FIELD_NAMES

    assert safe_harbor.SENSITIVE_FIELD_NAMES is SENSITIVE_FIELD_NAMES


# --- the report must be a real category ledger (review DEID-REPORT-CATEGORY) ---


@pytest.mark.parametrize("field,category", [
    ("ssn", "G"), ("social_security_number", "G"),
    ("email", "F"),
    ("dob", "C"), ("date_of_birth", "C"),
    ("phone", "D"),
    ("name", "A"), ("first_name", "A"), ("patient_name", "A"),
    ("address", "B"), ("zip_code", "B"),
])
def test_each_sensitive_field_reports_its_real_category(field, category):
    """Every structured key used to report as A regardless of content, so `ssn`
    counted as "names". That stops the report being a category ledger, which is
    what adr/0009's gate enumerates residual risk against."""
    _, report = scrub_structured({field: "whatever"})

    assert list(report.counts) == [f"{category}:field:{field}"]


def test_an_unclassified_sensitive_field_falls_back_to_R():
    # R is "any other unique identifying number, characteristic, or code" — the
    # honest bucket for something unclassified, rather than guessing a category.
    assert field_category("token") == "R"
    assert field_category("some_field_added_later") == "R"


def test_clinical_free_text_is_not_miscounted_as_an_identifier_type():
    # `notes` and model payloads are the CONTENT identifiers appear in, not one
    # of the eighteen categories. Visible in the ledger, not mislabelled.
    for field in ("notes", "prompt", "response", "body"):
        assert field_category(field) == "R"


def test_every_mapped_category_is_a_real_safe_harbor_letter():
    from libs.deid import safe_harbor

    assert set(safe_harbor._FIELD_CATEGORY.values()) <= set(IDENTIFIER_CATEGORIES)


def test_a_mixed_payload_reports_distinct_categories():
    _, report = scrub_structured({"ssn": "1", "email": "x@y.org", "dob": "2026-03-02"})

    assert {k.split(":", 1)[0] for k in report.counts} == {"G", "F", "C"}
