"""W10 Final Stage 3 — the PHI-safe logging boundary is now repository-wide,
not just new code (see docs/planning/phi-safe-logging-policy.md).

Two regression guards, mirroring tests/test_secrets_hygiene.py's static-scan
approach: a future `log.exception(...)` call anywhere in services/ would
reattach a raw traceback/exception-message risk (rule 5); a service whose
logging_config.py stops attaching PHISafeFilter would silently drop the
structured-data redaction backstop (rule 6). Both are cheap and infra-free.
"""
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


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [f for f in out.split("\0") if f]


def test_no_service_calls_log_exception_anymore():
    """log.exception()/logger.exception() attaches the real traceback and
    exception message — never filtered by PHISafeFilter (it only touches
    record.msg/record.args, not exc_info). Every prior call site was
    replaced with categorical `log.error(..., type(exc).__name__)` logging;
    this fails if a new one is ever added under services/."""
    offenders = []
    for path in _tracked_files():
        if not path.startswith("services/") or not path.endswith(".py"):
            continue
        if "/test" in path or path.endswith("_test.py"):
            continue
        text = (REPO / path).read_text(encoding="utf-8")
        if "log.exception(" in text or "logger.exception(" in text:
            offenders.append(path)
    assert not offenders, (
        f"{offenders} call log.exception()/logger.exception(), which logs a raw "
        f"traceback and exception message never filtered by PHISafeFilter — use "
        f"log.error(msg + ' error_type=%s', type(exc).__name__) instead."
    )


@pytest.mark.parametrize("service", SERVICES)
def test_every_service_attaches_the_safe_filter(service):
    mod = load_module(
        f"services/{service}/logging_config.py", f"logging_config_completeness_{service.replace('-', '_')}"
    )
    from libs.safe_logging import PHISafeFilter

    logger = mod.configure(f"completeness-test-{service}")
    assert any(isinstance(f, PHISafeFilter) for f in logger.filters), (
        f"services/{service}/logging_config.py's configure() does not attach PHISafeFilter"
    )
