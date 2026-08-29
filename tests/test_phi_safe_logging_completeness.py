"""W10 Final Stage 3 — the PHI-safe logging boundary is now repository-wide,
not just new code (see docs/planning/phi-safe-logging-policy.md).

Review findings (post-merge, PR #105):

- LOG-FILTER-PROPAGATION: PHISafeFilter attached only to a service's own
  logger object never actually runs for a record a child/module logger
  emits and propagates up — Python's logging module does not consult an
  ancestor LOGGER's filters for a propagated record, only that logger's own
  HANDLERS. The filter must sit on the handler(s) that actually emit.
- RAW-EXCEPTION-STRINGS: a handful of `log.error(msg, e)` call sites passed
  the caught exception object directly (formatting to str(e)) instead of
  the categorical `type(e).__name__` the rest of the codebase already uses
  — never caught by the earlier guard, which only looked for `.exception(`.

This file now covers both: a behavioral test per service proving PHI is
actually redacted from a CHILD logger's propagated, handler-emitted output,
and an AST-based scanner (not a substring/regex guess) that rejects every
raw-exception-logging shape the review named, repository-wide.
"""
import ast
import io
import logging
import pathlib
import subprocess

import pytest

from conftest import load_module

REPO = pathlib.Path(__file__).resolve().parents[1]

SERVICES = (
    "gateway",
    "intake-service",
    "eligibility-service",
    "records-service",
    "scheduling-service",
    "interop-service",
    "roi-service",
)

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
_LOGGER_NAMES = {"log", "logger"}


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [f for f in out.split("\0") if f]


def _is_logging_call(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _LOGGER_NAMES
    )


def _is_type_name_pattern(node) -> bool:
    """type(<anything>).__name__ — the one allowed way to reference a caught
    exception in a log call."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "type"
    )


def _is_str_or_repr_of_name(node, bound_names) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("str", "repr")
        and any(isinstance(a, ast.Name) and a.id in bound_names for a in node.args)
    )


def _is_traceback_call(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "traceback"
        and node.func.attr in ("format_exc", "print_exc")
    )


def _contains_raw_exception_reference(node, bound_names) -> bool:
    """True if `node` (an argument expression to a logging call) references
    a bound exception name unsafely — bare, via str()/repr(), inside an
    f-string, or via traceback.format_exc()/print_exc(). Two shapes are
    allowed and never descended into: `type(name).__name__` (the categorical
    form), and plain attribute access on the exception (`name.some_field`,
    e.g. a custom exception's own structured field like
    `existing_appointment_id`) — accessing one named field is not "logging
    the exception," unlike passing the object (or str()/repr() of it) whole."""
    if _is_type_name_pattern(node):
        return False
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound_names:
        return False
    if isinstance(node, ast.Name) and node.id in bound_names:
        return True
    if _is_str_or_repr_of_name(node, bound_names):
        return True
    if _is_traceback_call(node):
        return True
    for child in ast.iter_child_nodes(node):
        if _contains_raw_exception_reference(child, bound_names):
            return True
    return False


def scan_source_for_raw_exception_logging(source: str) -> list:
    """Returns a list of (lineno, reason) violations. Flags, at any logging
    call (`log`/`logger` . debug/info/warning/warn/error/critical/log/
    exception):
      - `.exception(...)` itself (always attaches a real traceback);
      - `exc_info=`/`stack_info=` truthy keyword arguments;
      - a bound exception variable passed bare, via str()/repr(), inside an
        f-string, or via traceback.format_exc()/print_exc().
    Allows `type(exc).__name__` (the categorical, safe form) anywhere.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node

    violations = []

    def _bound_names_in_scope(call_node):
        names = set()
        anc = call_node
        while hasattr(anc, "parent"):
            anc = anc.parent
            if isinstance(anc, ast.ExceptHandler) and anc.name:
                names.add(anc.name)
        return names

    for node in ast.walk(tree):
        if not _is_logging_call(node):
            continue

        if node.func.attr == "exception":
            violations.append((node.lineno, "uses .exception(), which always attaches a real traceback"))
            continue

        for kw in node.keywords:
            if kw.arg in ("exc_info", "stack_info") and not (
                isinstance(kw.value, ast.Constant) and kw.value.value is False
            ):
                violations.append((node.lineno, f"passes {kw.arg}=... (attaches traceback data)"))

        bound_names = _bound_names_in_scope(node)
        if not bound_names:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if _contains_raw_exception_reference(arg, bound_names):
                violations.append((node.lineno, "passes a caught exception's raw text instead of type(exc).__name__"))
                break

    return violations


# --- scanner self-test: proves the guard itself catches what it claims to --


@pytest.mark.parametrize("bad_source,expected_reason_substring", [
    ('try:\n    pass\nexcept Exception as e:\n    log.error("boom", e)\n', "raw text"),
    ('try:\n    pass\nexcept Exception as e:\n    log.error("boom: %s", str(e))\n', "raw text"),
    ('try:\n    pass\nexcept Exception as e:\n    log.warning("boom: %s", repr(e))\n', "raw text"),
    ('try:\n    pass\nexcept Exception as e:\n    log.error(f"boom: {e}")\n', "raw text"),
    ('try:\n    pass\nexcept Exception as e:\n    log.info("nested: %s", {"x": e})\n', "raw text"),
    ('try:\n    pass\nexcept Exception:\n    log.exception("boom")\n', "traceback"),
    ('import traceback\ntry:\n    pass\nexcept Exception as e:\n    log.error("boom: %s", traceback.format_exc())\n', "raw text"),
    ('try:\n    pass\nexcept Exception as e:\n    log.error("boom", exc_info=True)\n', "traceback"),
])
def test_scanner_flags_every_named_raw_exception_shape(bad_source, expected_reason_substring):
    violations = scan_source_for_raw_exception_logging(bad_source)
    assert violations, f"scanner failed to flag: {bad_source!r}"
    assert any(expected_reason_substring in reason for _, reason in violations)


@pytest.mark.parametrize("good_source", [
    'try:\n    pass\nexcept Exception as e:\n    log.error("boom error_type=%s", type(e).__name__)\n',
    'try:\n    pass\nexcept Exception as e:\n    log.error("boom id=%s error_type=%s", 42, type(e).__name__)\n',
    'log.info("ordinary message with id=%s", 42)\n',
    'try:\n    pass\nexcept Exception as e:\n    log.error("boom", exc_info=False)\n',
    # A custom exception's own structured, non-text field (e.g. an id) —
    # accessing one named attribute is not "logging the exception whole".
    'try:\n    pass\nexcept Exception as e:\n    log.warning("boom %s", e.existing_appointment_id)\n',
])
def test_scanner_allows_the_categorical_form_and_ordinary_logging(good_source):
    assert scan_source_for_raw_exception_logging(good_source) == []


def test_no_service_has_raw_exception_logging():
    """Repository-wide guard (review finding RAW-EXCEPTION-STRINGS): replaces
    the earlier substring-only `.exception(` check with the AST scanner
    above, covering every logging level and every shape the review named."""
    offenders = []
    for path in _tracked_files():
        if not path.startswith("services/") or not path.endswith(".py"):
            continue
        if "/test" in path or path.endswith("_test.py"):
            continue
        source = (REPO / path).read_text(encoding="utf-8")
        for lineno, reason in scan_source_for_raw_exception_logging(source):
            offenders.append(f"{path}:{lineno}: {reason}")
    assert not offenders, (
        "raw-exception-logging found (use log.error(msg + ' error_type=%s', "
        f"type(exc).__name__) instead):\n" + "\n".join(offenders)
    )


# --- LOG-FILTER-PROPAGATION: the filter must run at the emitting handler ---


@pytest.mark.parametrize("service", SERVICES)
def test_every_service_attaches_the_safe_filter_to_its_handlers(service):
    """Python does not run an ancestor LOGGER's filters for a record a
    child/module logger (e.g. `<service>.worker`) propagates up to it —
    only the HANDLERS that actually emit the record are consulted. A filter
    attached only to the top-level `service_name` logger object silently
    never runs for any other logger in that service. This proves PHI is
    actually redacted from a child logger's propagated, handler-emitted
    output, not just present as an object somewhere in the hierarchy."""
    mod = load_module(
        f"services/{service}/logging_config.py", f"logging_config_filter_prop_{service.replace('-', '_')}"
    )

    root_name = f"filter-prop-test-{service}"
    root_logger = mod.configure(root_name)

    # Route every handler's output through an in-memory stream so the
    # formatted record text can be inspected directly, without depending on
    # console/file output. Every active handler on the configured logger
    # (and, for services that call logging.basicConfig(), the real root
    # logger) must carry the filter — swap in a capturing stream on each.
    candidate_loggers = [root_logger, logging.getLogger()]
    streams = []
    for lg in candidate_loggers:
        for handler in lg.handlers:
            if not hasattr(handler, "stream"):
                continue  # e.g. pytest's own live-logging handler — not ours to test
            stream = io.StringIO()
            handler.stream = stream
            streams.append(stream)

    child_logger = logging.getLogger(f"{root_name}.worker")
    child_logger.propagate = True
    child_logger.error("patient event: %s", {"ssn": "123-45-6789", "name": "Jane Doe"})

    combined_output = "".join(s.getvalue() for s in streams)
    assert combined_output, f"no handler captured output for {service} — test setup issue"
    assert "123-45-6789" not in combined_output
    assert "Jane Doe" not in combined_output
    assert "***REDACTED***" in combined_output
