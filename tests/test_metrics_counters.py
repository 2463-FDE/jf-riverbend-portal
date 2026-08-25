"""Focused tests for libs.metrics.record_counter — Week 7's golden-signal
metric path. See docs/planning/policy-navigator-golden-signals-week7-08-25-2026.md.
"""
import logging

from libs.metrics import record_counter


def test_record_counter_emits_one_parseable_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="libs.metrics.counters"):
        record_counter("policy_navigator_termination_total", termination_reason="citation_invalid")

    [record] = caplog.records
    assert "metric=policy_navigator_termination_total" in record.message
    assert "value=1" in record.message
    assert "termination_reason=citation_invalid" in record.message


def test_record_counter_redacts_a_sensitive_label():
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("libs.metrics.counters")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        record_counter("some_metric", patient_name="Maria Gonzalez")
    finally:
        logger.removeHandler(handler)

    assert "Maria Gonzalez" not in stream.getvalue()
    assert "REDACTED" in stream.getvalue()


def test_record_counter_never_raises_even_if_the_logger_is_broken(monkeypatch):
    import libs.metrics.counters as counters_mod

    def _broken_info(*args, **kwargs):
        raise RuntimeError("logging backend unavailable")

    monkeypatch.setattr(counters_mod.log, "info", _broken_info)

    record_counter("some_metric", termination_reason="answered")  # must not raise
