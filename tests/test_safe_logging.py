"""Tests for the PHI-safe logging helpers (libs/safe_logging).

See docs/planning/phi-safe-logging-policy.md for the policy these implement.
"""
import logging

from libs.safe_logging import PHISafeFilter, REDACTED, get_safe_logger, redact


# --- redact() --------------------------------------------------------------


def test_redacts_known_sensitive_keys():
    payload = {"ssn": "111-22-3333", "dob": "1980-01-01", "name": "Jane Roe"}

    result = redact(payload)

    assert result == {"ssn": REDACTED, "dob": REDACTED, "name": REDACTED}


def test_leaves_non_sensitive_keys_untouched():
    payload = {"provider": "fake", "attempt": 2, "elapsed_ms": 150}

    result = redact(payload)

    assert result == payload


def test_redacts_recursively_through_nested_dicts_and_lists():
    payload = {
        "event": "intake_submitted",
        "patient": {"name": "Jane Roe", "insurance": {"member_id": "AET123"}},
        "notes_history": [{"notes": "chief complaint text"}, {"notes": "follow-up text"}],
    }

    result = redact(payload)

    assert result["event"] == "intake_submitted"
    assert result["patient"]["name"] == REDACTED
    assert result["patient"]["insurance"]["member_id"] == "AET123"  # not a sensitive key name
    assert result["notes_history"][0]["notes"] == REDACTED
    assert result["notes_history"][1]["notes"] == REDACTED


def test_key_matching_is_case_insensitive():
    payload = {"SSN": "111-22-3333", "Api_Key": "sk-abc123"}

    result = redact(payload)

    assert result == {"SSN": REDACTED, "Api_Key": REDACTED}


def test_does_not_mutate_input():
    payload = {"ssn": "111-22-3333", "nested": {"dob": "1980-01-01"}}
    original = {"ssn": "111-22-3333", "nested": {"dob": "1980-01-01"}}

    redact(payload)

    assert payload == original


def test_bare_strings_and_numbers_pass_through_unchanged():
    assert redact("just a string, not a dict") == "just a string, not a dict"
    assert redact(42) == 42
    assert redact(None) is None


# --- PHISafeFilter -----------------------------------------------------


def _make_record(msg, args):
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=args, exc_info=None
    )


def test_filter_redacts_dict_args():
    # A single dict passed as the logging arg is unwrapped by LogRecord itself
    # (stdlib %-style dict-arg convention), so record.args ends up as the dict,
    # not a 1-tuple containing it — this is the case PHISafeFilter's
    # `isinstance(record.args, dict)` branch exists for.
    record = _make_record("event=%s", {"ssn": "111-22-3333", "provider": "fake"})

    assert PHISafeFilter().filter(record) is True
    assert record.args == {"ssn": REDACTED, "provider": "fake"}


def test_filter_redacts_list_args():
    record = _make_record("event=%s", ([{"dob": "1980-01-01"}],))

    PHISafeFilter().filter(record)

    assert record.args[0] == [{"dob": REDACTED}]


def test_filter_leaves_scalar_args_untouched():
    record = _make_record("attempt=%s provider=%s", (2, "fake"))

    PHISafeFilter().filter(record)

    assert record.args == (2, "fake")


def test_filter_redacts_dict_or_list_msg():
    record = _make_record({"ssn": "111-22-3333"}, None)

    PHISafeFilter().filter(record)

    assert record.msg == {"ssn": REDACTED}


def test_filter_always_returns_true_so_the_record_is_still_emitted():
    record = _make_record("plain message, no args", None)

    assert PHISafeFilter().filter(record) is True


# --- get_safe_logger() -----------------------------------------------


def test_get_safe_logger_attaches_exactly_one_filter_even_when_called_twice():
    logger_a = get_safe_logger("tests.safe_logging.dedup_check")
    logger_b = get_safe_logger("tests.safe_logging.dedup_check")

    assert logger_a is logger_b
    phi_filters = [f for f in logger_a.filters if isinstance(f, PHISafeFilter)]
    assert len(phi_filters) == 1


def test_get_safe_logger_does_not_duplicate_handlers_on_repeat_calls():
    get_safe_logger("tests.safe_logging.handler_check")
    logger = get_safe_logger("tests.safe_logging.handler_check")

    assert len(logger.handlers) == 1


def test_get_safe_logger_respects_log_level_env_var(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    logger = get_safe_logger("tests.safe_logging.level_check")

    assert logger.level == logging.WARNING
