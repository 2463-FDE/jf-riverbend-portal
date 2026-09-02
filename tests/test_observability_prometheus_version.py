"""W10 Final Stage 7 review fix OBS-B01 — the observability profile's
Prometheus image pin must be a version new enough to support the
`http_headers` scrape-config field (used by the records-service/
scheduling-service/roi-service scrape jobs to present X-Internal-Token),
and every committed reference to that pin must agree — a stale reference in
docs or the promtool-test instructions is exactly how the prior mismatch
(v2.54.1) went unnoticed.

Pure static/parsing checks — no docker/network access, so these run in the
regular (non-integration) suite.
"""
import re
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_RUNBOOK = _ROOT / "docs" / "runbook.md"
_PROMTOOL_TEST_FILE = _ROOT / "observability" / "promtool_tests" / "alert_rules_test.yml"
_PROMETHEUS_TEMPLATE = _ROOT / "observability" / "prometheus" / "prometheus.yml.template"

# http_headers on a scrape_config was introduced in Prometheus 2.47 and is
# still present at 2.55; OBS-B01's floor is 2.55.0 specifically (the pin
# that replaced the incompatible v2.54.1).
_MINIMUM_VERSION = (2, 55, 0)

_VERSION_RE = re.compile(r"prom/prometheus:v(\d+)\.(\d+)\.(\d+)")

# Scrape jobs that must present the internal-service-token header. Gateway
# is deliberately excluded: its /metrics is unauthenticated, matching its
# own /healthz precedent (see libs/metrics/http.py, services/gateway/app.py).
_TOKEN_GUARDED_JOBS = (
    "records-service", "scheduling-service", "roi-service",
    # W10 metrics Stage 1: eligibility-service exposes /metrics behind the
    # same internal-token guard as its other routes, so Prometheus must
    # present the header for it too.
    "eligibility-service",
)


def _compose_prometheus_image():
    services = yaml.safe_load(_COMPOSE.read_text())["services"]
    return services["prometheus"]["image"]


def _all_version_references():
    """Every place in the repo that pins/prints a prom/prometheus tag."""
    refs = {}
    refs["docker-compose.yml"] = _compose_prometheus_image()
    for path in (_RUNBOOK, _PROMTOOL_TEST_FILE):
        text = path.read_text()
        match = _VERSION_RE.search(text)
        assert match, f"{path} has no prom/prometheus:vX.Y.Z reference to check"
        refs[str(path.relative_to(_ROOT))] = f"prom/prometheus:v{'.'.join(match.groups())}"
    return refs


def test_the_pinned_prometheus_version_meets_the_http_headers_floor():
    image = _compose_prometheus_image()
    match = _VERSION_RE.search(image)
    assert match, f"docker-compose.yml's prometheus image {image!r} is not a prom/prometheus:vX.Y.Z pin"
    version = tuple(int(g) for g in match.groups())
    assert version >= _MINIMUM_VERSION, (
        f"prometheus is pinned to {image}, which is older than the "
        f"http_headers floor v{'.'.join(map(str, _MINIMUM_VERSION))} (OBS-B01)"
    )


def test_no_committed_reference_to_the_incompatible_v2_54_1_pin():
    for path in (_COMPOSE, _RUNBOOK, _PROMTOOL_TEST_FILE):
        assert "v2.54.1" not in path.read_text(), f"{path} still references the incompatible v2.54.1 pin"


def test_every_committed_prometheus_version_reference_agrees():
    refs = _all_version_references()
    distinct = set(refs.values())
    assert len(distinct) == 1, f"prom/prometheus version references disagree: {refs}"


# gateway's /metrics is unauthenticated (matches its own /healthz).
# pushgateway (W10 Metrics Stage 5) has no auth of its own either — it is a
# local-only observability-profile service with no internal-service-token
# concept, holding only the sanitized numeric gauges libs/rag_eval_metrics
# pushed to it.
_UNAUTHENTICATED_JOBS = ("gateway", "pushgateway")


def test_the_token_guarded_scrape_jobs_still_carry_the_internal_token_header():
    config = yaml.safe_load(_PROMETHEUS_TEMPLATE.read_text())
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}

    assert set(jobs) == {*_UNAUTHENTICATED_JOBS, *_TOKEN_GUARDED_JOBS}, (
        "the set of scrape jobs changed — update _TOKEN_GUARDED_JOBS/_UNAUTHENTICATED_JOBS if intentional"
    )

    for name in _TOKEN_GUARDED_JOBS:
        headers = jobs[name].get("http_headers", {})
        assert "X-Internal-Token" in headers, f"{name} scrape job lost its X-Internal-Token header config"
        assert headers["X-Internal-Token"]["values"] == ["${INTERNAL_SERVICE_TOKEN}"], (
            f"{name} scrape job's X-Internal-Token no longer references the "
            f"${{INTERNAL_SERVICE_TOKEN}} placeholder — the rendering step in "
            f"docker-compose.yml's prometheus service substitutes this at "
            f"container startup, never a literal secret committed here"
        )

    for name in _UNAUTHENTICATED_JOBS:
        assert "http_headers" not in jobs[name]


def test_the_pushgateway_scrape_job_honors_pushed_labels():
    """Without honor_labels, Prometheus's own job/instance labels would
    silently overwrite the job="rag_eval"/corpus="..." labels each publish
    already set via its grouping key — collapsing both evaluation corpora's
    series into one indistinguishable set."""
    config = yaml.safe_load(_PROMETHEUS_TEMPLATE.read_text())
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert jobs["pushgateway"].get("honor_labels") is True
