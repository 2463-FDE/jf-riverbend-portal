"""Validation tests for the multi-step intake payload (intake-service/schemas.py)."""
from conftest import load_module
import pytest
from pydantic import ValidationError

schemas = load_module("services/intake-service/schemas.py", "intake_schemas")


def test_minimal_valid_intake():
    req = schemas.IntakeRequest(
        demographics={"name": "Jane Roe"}, consents=["npp_ack", "treatment_consent"]
    )
    assert req.demographics.name == "Jane Roe"
    assert req.demographics.created_via == "self_service"
    assert req.consents == ["npp_ack", "treatment_consent"]


def test_omitted_consents_field_is_rejected_not_defaulted():
    # Round-16 review (2026-08-06): consents used to default to
    # ["npp_ack", "treatment_consent"] when the field was left out entirely
    # — round-15 closed consents:[] but missed this. Omitting the field
    # must be exactly as invalid as sending [], not silently treated as "the
    # patient signed both."
    with pytest.raises(ValidationError):
        schemas.IntakeRequest(demographics={"name": "Jane Roe"})


def test_full_intake_with_insurance():
    req = schemas.IntakeRequest(
        demographics={"name": "John Doe", "dob": "1980-01-01", "ssn": "111-22-3333"},
        insurance={"payer_name": "Aetna", "member_id": "AET123", "plan_type": "PPO"},
        consents=["npp_ack", "treatment_consent"],
    )
    assert req.insurance.payer_name == "Aetna"
    assert req.consents == ["npp_ack", "treatment_consent"]


def test_blank_name_rejected():
    with pytest.raises(ValidationError):
        schemas.IntakeRequest(demographics={"name": "   "})


def test_missing_demographics_rejected():
    with pytest.raises(ValidationError):
        schemas.IntakeRequest(consents=["npp_ack"])


# --- Week 6: structured demographics/contact, additive + backward compatible ----


def test_legacy_name_and_combined_address_pass_through_unchanged():
    """A caller that only ever sends the old combined fields must see identical
    stored behavior: `name`/`address` unchanged, new structured columns unset."""
    req = schemas.IntakeRequest(
        demographics={"name": "Jane Roe", "address": "118 Maple Ave, Beverly Hills, CA 90210"},
        consents=["npp_ack", "treatment_consent"],
    )
    demo = req.demographics
    assert demo.name == "Jane Roe"
    assert demo.address == "118 Maple Ave, Beverly Hills, CA 90210"
    assert demo.first_name is None
    assert demo.last_name is None
    assert demo.city is None
    assert demo.state is None
    assert demo.zip_code is None


def test_structured_name_derives_legacy_name():
    req = schemas.IntakeRequest(
        demographics={"first_name": "Jane", "last_name": "Roe"},
        consents=["npp_ack", "treatment_consent"],
    )
    demo = req.demographics
    assert demo.first_name == "Jane"
    assert demo.last_name == "Roe"
    assert demo.name == "Jane Roe"  # derived legacy compatibility value


def test_structured_address_derives_legacy_combined_address():
    req = schemas.IntakeRequest(
        demographics={
            "first_name": "Jane",
            "last_name": "Roe",
            "address": "118 Maple Ave",
            "city": "Beverly Hills",
            "state": "CA",
            "zip_code": "90210",
        },
        consents=["npp_ack", "treatment_consent"],
    )
    demo = req.demographics
    assert demo.address == "118 Maple Ave, Beverly Hills, CA 90210"
    assert demo.city == "Beverly Hills"
    assert demo.state == "CA"
    assert demo.zip_code == "90210"


def test_zip_code_preserves_leading_zeros_and_plus_four():
    req = schemas.IntakeRequest(
        demographics={"name": "Jane Roe", "zip_code": "02139-1234"},
        consents=["npp_ack", "treatment_consent"],
    )
    assert req.demographics.zip_code == "02139-1234"


def test_structured_address_with_missing_parts_has_no_malformed_commas():
    # Only city supplied — street/state/zip blank — must not leave stray
    # ", ," or trailing/leading commas in the composed legacy address.
    req = schemas.IntakeRequest(
        demographics={"name": "Jane Roe", "city": "Riverbend"},
        consents=["npp_ack", "treatment_consent"],
    )
    assert req.demographics.address == "Riverbend"
    assert ",," not in (req.demographics.address or "")
    assert not (req.demographics.address or "").startswith(",")
    assert not (req.demographics.address or "").endswith(",")


def test_explicit_name_takes_precedence_over_structured_name_when_both_given():
    req = schemas.IntakeRequest(
        demographics={"name": "Legacy Caller Name", "first_name": "Jane", "last_name": "Roe"},
        consents=["npp_ack", "treatment_consent"],
    )
    assert req.demographics.name == "Legacy Caller Name"
    # Structured fields are still stored even though `name` wasn't derived from them.
    assert req.demographics.first_name == "Jane"
    assert req.demographics.last_name == "Roe"


def test_blank_structured_name_rejected():
    with pytest.raises(ValidationError):
        schemas.IntakeRequest(demographics={"first_name": "  ", "last_name": "  "})


def test_whitespace_only_optional_contact_fields_are_normalized_to_none():
    req = schemas.IntakeRequest(
        demographics={"name": "Jane Roe", "city": "   ", "state": "  ", "zip_code": " "},
        consents=["npp_ack", "treatment_consent"],
    )
    demo = req.demographics
    assert demo.city is None
    assert demo.state is None
    assert demo.zip_code is None


# --- Week 1 catch-up, PR #20 round-3 review: created_via is untrusted input --


def test_created_via_defaults_are_trusted():
    for value in ("self_service", "front_desk"):
        req = schemas.IntakeRequest(
            demographics={"name": "Jane Roe", "created_via": value},
            consents=["npp_ack", "treatment_consent"],
        )
        assert req.demographics.created_via == value


def test_created_via_outside_known_set_is_normalized_to_unknown():
    # Demographics.created_via is a plain client-controlled string, not an
    # enum — a bad client (or a future UI bug) could put arbitrary/PHI-shaped
    # text in it, and it flows straight into an INFO log line
    # (services/intake-service/app.py::_intake_log_summary) right next to the
    # new patient_id. Anything outside the documented self_service/front_desk
    # set must be normalized before it's trusted anywhere, logging included.
    req = schemas.IntakeRequest(
        demographics={"name": "Jane Roe", "created_via": "patient is Jane Roe, SSN 111-22-3333"},
        consents=["npp_ack", "treatment_consent"],
    )
    assert req.demographics.created_via == "unknown"


# --- Week 2-3 catch-up: adr/0004/RIV-160 duplicate-override contract -------


def test_duplicate_override_and_confirmed_by_no_longer_exist_on_the_schema():
    # Round-10 review (2026-08-05): both fields removed entirely — not just
    # restricted to "create_new" (round-8's fix). intake-service has no auth
    # dependency, so nothing stopped any caller from both bypassing the
    # exact-match block AND forging who supposedly confirmed it (see
    # services/intake-service/app.py::create_intake). There is no
    # model_config extra="forbid" here, so passing either as a raw kwarg is
    # silently ignored rather than raising — asserting neither is present on
    # the parsed model either way.
    req = schemas.IntakeRequest(
        demographics={"name": "Jane Roe"},
        consents=["npp_ack", "treatment_consent"],
        duplicate_override="create_new",
        confirmed_by="dr.smith",
    )
    assert not hasattr(req, "duplicate_override")
    assert not hasattr(req, "confirmed_by")


# --- Round-15 review (2026-08-06): consents is a trust boundary, not just a
# UI button state (frontend/app/intake/page.tsx's consentsOk gates the
# submit button, but that's the browser, not this contract) -----------------


def test_empty_consents_is_rejected():
    with pytest.raises(ValidationError):
        schemas.IntakeRequest(demographics={"name": "Jane Roe"}, consents=[])


def test_missing_one_required_consent_is_rejected():
    # Both npp_ack and treatment_consent are required — supplying only one
    # must fail the same way supplying neither does.
    with pytest.raises(ValidationError):
        schemas.IntakeRequest(demographics={"name": "Jane Roe"}, consents=["npp_ack"])
    with pytest.raises(ValidationError):
        schemas.IntakeRequest(demographics={"name": "Jane Roe"}, consents=["treatment_consent"])


def test_unknown_consent_kind_is_rejected():
    # A typo'd or made-up consent name must fail loudly (422) rather than
    # silently persist as an untracked string in the consents table.
    with pytest.raises(ValidationError):
        schemas.IntakeRequest(
            demographics={"name": "Jane Roe"},
            consents=["npp_ack", "treatment_consent", "not_a_real_consent"],
        )


def test_both_optional_consent_kinds_are_accepted_alongside_the_required_ones():
    req = schemas.IntakeRequest(
        demographics={"name": "Jane Roe"},
        consents=["npp_ack", "treatment_consent", "financial_agreement", "communications_consent"],
    )
    assert set(req.consents) == {
        "npp_ack", "treatment_consent", "financial_agreement", "communications_consent",
    }


# NOTE (coverage gap, deliberate): nothing here asserts SSN format or that DOB
# is a real date — the service does neither (no input normalization). Unlike
# when this note was first written, /intake does now have a match-key check
# (adr/0004/RIV-160, see tests/test_intake_endpoint.py) — but it is a
# deterministic (dob, ssn) lookup, not the fuzzy-name/no-input-normalization
# gap this note originally meant. See SEEDED-DEBT.
