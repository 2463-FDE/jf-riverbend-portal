"""libs/eligibility_agent/response_contract.py — intent-to-tool selection and
deterministic rendering of an eligibility answer.

Pure unit coverage: no model, no payer, no network. The rendering assertions
are exact strings on purpose — the whole point of moving this out of the
model is that each outcome has one correct wording, so a change to any of
them should be a deliberate edit here rather than a silent drift in prose.
"""
import pytest

from libs.eligibility_agent.eligibility_tool import COVERAGE_TOOL_NAME, VERIFY_TOOL_NAME
from libs.eligibility_agent.response_contract import (
    MAX_SENTENCES,
    Intent,
    classify_intent,
    render_reply,
)

# --------------------------------------------------------------------------- #
# Intent -> tool selection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "message",
    [
        "Is insurance valid?",
        "is it active?",
        "verify eligibility",
        "Can you verify this patient's eligibility now?",
        "please recheck eligibility",
    ],
)
def test_a_verification_question_reaches_only_the_verify_tool(message):
    decision = classify_intent(message)
    assert decision.intent is Intent.VERIFY
    assert decision.tool_names == (VERIFY_TOOL_NAME,)
    assert [spec["name"] for spec in decision.tool_specs] == [VERIFY_TOOL_NAME]


def test_am_i_covered_is_a_verification_request():
    """INTENT-COVERED-GAP: the canonical front-desk phrasing. Left
    unclassified it would have fallen through to UNSPECIFIED and offered
    both tools, which is exactly the ambiguity this contract removes."""
    decision = classify_intent("am I covered?")
    assert decision.intent is Intent.VERIFY
    assert decision.tool_names == (VERIFY_TOOL_NAME,)
    assert [spec["name"] for spec in decision.tool_specs] == [VERIFY_TOOL_NAME]


@pytest.mark.parametrize(
    "message",
    ["am I covered?", "is this patient covered?", "is coverage active?", "coverage active"],
)
def test_the_other_covered_phrasings_are_verification_requests_too(message):
    decision = classify_intent(message)
    assert decision.intent is Intent.VERIFY
    assert decision.tool_names == (VERIFY_TOOL_NAME,)


@pytest.mark.parametrize(
    "message",
    [
        "What does my plan cover for an annual physical?",
        # COVERED-BENEFITS-MISROUTE: these all contain "covered" but ask what
        # the plan PAYS FOR, not whether this patient's insurance is in
        # force. Neither tool answers them, so neither may be narrowed to —
        # least of all a live payer verification, which would answer a
        # question nobody asked and read as though it had.
        "Is the flu shot covered?",
        "What services are covered?",
        "Is physical therapy covered?",
        "What benefits are covered?",
    ],
)
def test_a_benefit_or_service_question_is_never_routed_to_verification(message):
    decision = classify_intent(message)
    assert decision.intent is Intent.UNSPECIFIED
    # Explicitly NOT narrowed to live verification — the tool set is left as
    # it was, which is what UNSPECIFIED means.
    assert decision.tool_names != (VERIFY_TOOL_NAME,)
    assert set(decision.tool_names) == {VERIFY_TOOL_NAME, COVERAGE_TOOL_NAME}


@pytest.mark.parametrize(
    "message",
    [
        "What coverage is on file?",
        "which payer is on file?",
        "what plan does this patient have?",
        "what's the stored status?",
        "what member id is on record?",
    ],
)
def test_a_stored_record_question_reaches_only_the_coverage_tool(message):
    decision = classify_intent(message)
    assert decision.intent is Intent.COVERAGE
    assert decision.tool_names == (COVERAGE_TOOL_NAME,)
    assert [spec["name"] for spec in decision.tool_specs] == [COVERAGE_TOOL_NAME]


def test_both_tools_run_only_for_an_explicitly_combined_request():
    decision = classify_intent("What coverage is on file, and is it still active?")
    assert decision.intent is Intent.BOTH
    assert set(decision.tool_names) == {VERIFY_TOOL_NAME, COVERAGE_TOOL_NAME}


def test_a_single_request_naming_a_record_field_is_not_a_combined_request():
    """"check a different member id" carries both vocabularies but asks for
    one thing — it must not silently trigger a stored-record lookup as well."""
    decision = classify_intent("check a different member id")
    assert decision.intent is Intent.VERIFY
    assert decision.tool_names == (VERIFY_TOOL_NAME,)


@pytest.mark.parametrize("message", ["hi", "anything else?", "What does my plan cover for an annual physical?"])
def test_an_unrecognised_ask_leaves_the_existing_tool_set_untouched(message):
    decision = classify_intent(message)
    assert decision.intent is Intent.UNSPECIFIED
    assert set(decision.tool_names) == {VERIFY_TOOL_NAME, COVERAGE_TOOL_NAME}


# --------------------------------------------------------------------------- #
# Deterministic rendering, one exact wording per outcome
# --------------------------------------------------------------------------- #


def test_verified_active_states_the_result_and_the_date():
    reply = render_reply(
        verify_payload={"outcome": "verified", "status": "active", "as_of": "2026-08-23T14:02:00+00:00"}
    )
    assert reply == "Eligibility is active as of August 23, 2026."


def test_verified_inactive_is_stated_as_inactive():
    reply = render_reply(
        verify_payload={"outcome": "verified", "status": "inactive", "as_of": "2026-08-23T14:02:00+00:00"}
    )
    assert reply == "Eligibility is inactive as of August 23, 2026."


def test_simulated_never_reads_as_a_completed_payer_check():
    reply = render_reply(verify_payload={"outcome": "simulated", "status": "active", "as_of": None})
    assert reply == (
        "A current eligibility check was not run because this is a synthetic training environment. "
        "Coverage on file is active."
    )
    # The load-bearing part: nothing here can be read as "we just verified it".
    assert "is active as of" not in reply


def test_unavailable_with_an_unknown_record_says_so_and_what_to_do():
    reply = render_reply(verify_payload={"outcome": "unavailable", "status": "unknown", "as_of": None})
    assert reply == (
        "Eligibility could not be verified right now. "
        "The coverage record on file is unknown. "
        "Try again later or contact the payer."
    )


def test_a_stored_lookup_names_the_payer_and_labels_the_status_as_stored():
    reply = render_reply(
        coverage_payload={
            "has_coverage_on_file": True,
            "payer_name": "UnitedHealthcare",
            "plan_type": "HMO",
            "status": "active",
            "member_id_masked": "****6789",
        }
    )
    assert reply == "Coverage on file is UnitedHealthcare HMO. Its stored status is active."


def test_pending_and_stale_are_distinct_from_a_returned_status():
    pending = render_reply(verify_payload={"outcome": "verified", "status": "pending", "as_of": None})
    assert pending == "An eligibility check is in progress and has not returned a result yet."

    stale = render_reply(
        verify_payload={"outcome": "verified", "status": "stale", "as_of": "2026-07-01T09:00:00+00:00"}
    )
    assert stale == "Eligibility could not be re-checked just now; the last known result is from July 1, 2026."


def test_no_coverage_on_file_is_stated_plainly():
    reply = render_reply(coverage_payload={"has_coverage_on_file": False})
    assert reply == "No insurance coverage is on file for this visit."


def test_no_tool_result_renders_nothing_so_the_caller_keeps_the_models_answer():
    assert render_reply() is None


# --------------------------------------------------------------------------- #
# Safety: what must never reach the UI
# --------------------------------------------------------------------------- #


def test_member_id_is_withheld_unless_expressly_requested():
    payload = {
        "has_coverage_on_file": True,
        "payer_name": "UnitedHealthcare",
        "plan_type": "HMO",
        "status": "active",
        "member_id_masked": "****6789",
    }
    assert "6789" not in render_reply(coverage_payload=payload)
    # Expressly asked for — and then only the already-masked stored value.
    asked = render_reply(coverage_payload=payload, include_member_id=True)
    assert asked.endswith("The member ID on file is ****6789.")


def test_a_requested_member_id_is_only_ever_the_masked_stored_value():
    """The renderer has no access to an unmasked id: it reads
    `member_id_masked` and nothing else, so a full number in the payload's
    other fields cannot be surfaced by asking for the member id."""
    reply = render_reply(
        coverage_payload={
            "has_coverage_on_file": True,
            "payer_name": "Kaiser",
            "status": "active",
            "member_id_masked": "****6789",
            "member_id": "123456789",
        },
        include_member_id=True,
    )
    assert "123456789" not in reply
    assert "****6789" in reply


def test_an_unmasked_member_id_is_withheld_even_when_expressly_requested():
    """"already safely masked" is verified, not assumed: a stored value
    carrying no mask character is dropped rather than printed."""
    reply = render_reply(
        coverage_payload={
            "has_coverage_on_file": True,
            "payer_name": "Kaiser",
            "status": "active",
            "member_id_masked": "123456789",
        },
        include_member_id=True,
    )
    assert "123456789" not in reply
    assert "member ID" not in reply


def test_markdown_and_emoji_in_a_stored_field_never_reach_the_reply():
    reply = render_reply(
        coverage_payload={
            "has_coverage_on_file": True,
            "payer_name": "| Payer | Plan |\n|---|---|\n**Aetna** ✅",
            "plan_type": "# PPO 🎉",
            "status": "active",
        }
    )
    for forbidden in ("|", "#", "*", "`", "✅", "🎉", "\n"):
        assert forbidden not in reply
    assert "Aetna" in reply and "PPO" in reply


def test_a_reply_never_carries_raw_payload_syntax():
    reply = render_reply(
        verify_payload={"outcome": "verified", "status": "active", "as_of": "2026-08-23T14:02:00+00:00"},
        coverage_payload={"has_coverage_on_file": True, "payer_name": "Aetna", "status": "active"},
    )
    for forbidden in ("{", "}", '"outcome"', "has_coverage_on_file", "2026-08-23T14:02:00"):
        assert forbidden not in reply


def test_a_combined_reply_stays_within_the_sentence_budget():
    reply = render_reply(
        verify_payload={"outcome": "unavailable", "status": "stale", "as_of": None},
        coverage_payload={
            "has_coverage_on_file": True,
            "payer_name": "UnitedHealthcare",
            "plan_type": "HMO",
            "status": "stale",
            "member_id_masked": "****6789",
        },
        include_member_id=True,
    )
    assert reply.count(".") <= MAX_SENTENCES
    assert len(reply.split(". ")) <= MAX_SENTENCES


def test_an_unparseable_verification_time_omits_the_date_rather_than_printing_it():
    reply = render_reply(verify_payload={"outcome": "verified", "status": "active", "as_of": "not-a-timestamp"})
    assert reply == "Eligibility is active."
