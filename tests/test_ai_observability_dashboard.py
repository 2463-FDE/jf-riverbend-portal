"""W10 metrics Stage 2 — static validation of the AI Agent Observability
dashboard and its three new alert rules.

No Grafana/Prometheus process is started here; `promtool test rules` (run
manually per observability/promtool_tests/alert_rules_test.yml's own header)
is the actual behavioral proof that each rule fires and stays quiet. This
file catches the class of error promtool can't: a typo'd metric name, a
duplicate panel id, an overflowing grid, a datasource uid that doesn't match
what's actually provisioned — a plausible-looking dashboard that would
silently show "No data" for every panel.
"""
import json
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DASHBOARD = _ROOT / "observability" / "grafana" / "dashboards" / "ai-agent-observability.json"
_EXISTING_DASHBOARD = _ROOT / "observability" / "grafana" / "dashboards" / "riverbend-services.json"
_DATASOURCES = _ROOT / "observability" / "grafana" / "provisioning" / "datasources" / "datasources.yml"
_ALERT_RULES = _ROOT / "observability" / "prometheus" / "alert_rules.yml"
_AI_METRICS_SRC = _ROOT / "libs" / "metrics" / "ai.py"

_NEW_ALERT_NAMES = (
    "AiProviderErrorOrFallbackRatioSpike",
    "EligibilityCircuitOpen",
    "AiAgentMaxTurnsOrCitationInvalidSpike",
)


def _dashboard():
    return json.loads(_DASHBOARD.read_text())


def _known_datasource_uids():
    config = yaml.safe_load(_DATASOURCES.read_text())
    return {ds["uid"] for ds in config["datasources"]}


def _ai_metric_base_names():
    """The exact metric name string literals declared in libs/metrics/ai.py —
    read from source, not hand-duplicated here, so this can't silently drift
    out of sync with a rename. Matches the first quoted string following
    each Counter(/Histogram(/Gauge( constructor call, whatever its own
    naming suffix happens to be (not every metric ends in _total/_seconds)."""
    text = _AI_METRICS_SRC.read_text()
    return set(re.findall(r'(?:Counter|Histogram|Gauge)\(\s*\n?\s*"([a-z_]+)"', text))


def _promql_metric_tokens(expr: str):
    """Every bare Prometheus metric-name-shaped identifier in a PromQL
    expression — deliberately excluding label lists. `by (a, b)` /
    `without (a, b)` group labels, not metrics, so their parenthesized
    contents are stripped first; `{label="value"}` selectors are stripped
    the same way. What's left is scanned for identifiers not immediately
    followed by '(' (a function call) and not a PromQL/aggregation keyword.
    """
    keywords = {
        "sum", "rate", "by", "on", "or", "and", "unless", "histogram_quantile",
        "le", "avg", "count", "min", "max", "without",
    }
    stripped = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", expr)
    stripped = re.sub(r"\{[^}]*\}", " ", stripped)
    tokens = re.findall(r"\b[a-z][a-z0-9_]*\b(?!\s*\()", stripped)
    return {t for t in tokens if t not in keywords}


def test_dashboard_json_is_well_formed_and_uid_is_unique():
    dash = _dashboard()
    assert dash["uid"] == "ai-agent-observability"
    existing = json.loads(_EXISTING_DASHBOARD.read_text())
    assert dash["uid"] != existing["uid"]


def test_every_panel_id_is_unique():
    dash = _dashboard()
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids)), f"duplicate panel ids: {ids}"


def test_no_panel_overflows_the_24_column_grid():
    dash = _dashboard()
    for panel in dash["panels"]:
        gp = panel["gridPos"]
        assert gp["x"] + gp["w"] <= 24, f"panel {panel['id']} ({panel['title']!r}) overflows: {gp}"


def test_every_panel_datasource_is_actually_provisioned():
    dash = _dashboard()
    known = _known_datasource_uids()
    for panel in dash["panels"]:
        uid = panel["datasource"]["uid"]
        assert uid in known, f"panel {panel['id']} references unknown datasource uid {uid!r}"
        for target in panel.get("targets", []):
            target_uid = target["datasource"]["uid"]
            assert target_uid in known, (
                f"panel {panel['id']} target {target.get('refId')} references unknown datasource {target_uid!r}"
            )


def test_every_prometheus_target_references_a_real_ai_metric():
    """Catches the class of bug promtool cannot: a typo'd metric name in a
    dashboard panel, which Grafana would silently render as an empty graph
    rather than an error."""
    dash = _dashboard()
    known_bases = _ai_metric_base_names()
    assert known_bases, "expected to find at least one metric name in libs/metrics/ai.py"
    # Histograms expose _bucket/_sum/_count siblings a dashboard may query.
    histogram_bases = {"bedrock_call_duration_seconds", "agent_citations_per_answer",
                       "agent_review_duration_seconds"}
    known = set(known_bases)
    for base in histogram_bases & known_bases:
        known |= {f"{base}_bucket", f"{base}_sum", f"{base}_count"}

    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            if target["datasource"]["type"] != "prometheus":
                continue
            found = _promql_metric_tokens(target["expr"])
            unknown = found - known
            assert not unknown, (
                f"panel {panel['id']} target {target.get('refId')} references unrecognized "
                f"metric-shaped token(s) {unknown} not declared in libs/metrics/ai.py: {target['expr']!r}"
            )


def test_the_loki_panel_never_filters_on_prompt_or_response_content():
    """The dashboard's one Loki panel must stay a categorical filter (level,
    service, known error phrases) — never a query shaped like it is
    searching rendered prompt/response/document text."""
    dash = _dashboard()
    forbidden = ("prompt", "response_text", "generated_text", "quote=", "citation_text")
    logs_panels = [p for p in dash["panels"] if p["type"] == "logs"]
    assert logs_panels, "expected at least one Loki logs panel per the stage requirement"
    for panel in logs_panels:
        for target in panel["targets"]:
            expr = target["expr"].lower()
            for word in forbidden:
                assert word not in expr, f"logs panel query looks content-shaped: {target['expr']!r}"


def test_no_pending_review_backlog_panel_is_present():
    """Deliberate absence, asserted so it isn't added casually — Stage 1
    exposes no truthful backlog gauge (see libs/metrics/ai.py), and a panel
    querying a nonexistent series would just show 'No data' forever."""
    dash = _dashboard()
    for panel in dash["panels"]:
        assert "backlog" not in panel["title"].lower()


# --- alert rules -------------------------------------------------------


def _alert_rules():
    data = yaml.safe_load(_ALERT_RULES.read_text())
    rules = {}
    for group in data["groups"]:
        for rule in group["rules"]:
            rules[rule["alert"]] = rule
    return rules


@pytest.mark.parametrize("name", _NEW_ALERT_NAMES)
def test_each_new_alert_rule_is_present_and_well_formed(name):
    rules = _alert_rules()
    assert name in rules, f"{name} is missing from {_ALERT_RULES}"
    rule = rules[name]
    assert rule.get("expr"), f"{name} has no expr"
    assert rule.get("for"), f"{name} has no 'for' duration — would fire on a single noisy scrape"
    assert rule.get("labels", {}).get("severity"), f"{name} has no severity label"
    annotations = rule.get("annotations", {})
    assert annotations.get("summary") and annotations.get("description"), (
        f"{name} is missing a summary or description annotation"
    )


def test_the_alert_file_states_these_are_local_prometheus_rules_only():
    text = _ALERT_RULES.read_text().lower()
    assert "alertmanager" in text
    assert "no external paging" in text or "no production paging" in text


# --- Loki correlation_id derived field (pulled forward from Stage 3) -----


def _loki_datasource():
    config = yaml.safe_load(_DATASOURCES.read_text())
    matches = [ds for ds in config["datasources"] if ds["uid"] == "loki_ds"]
    assert len(matches) == 1
    return matches[0]


def test_loki_has_a_correlation_id_derived_field():
    loki = _loki_datasource()
    fields = loki.get("jsonData", {}).get("derivedFields", [])
    by_name = {f["name"]: f for f in fields}
    assert "correlation_id" in by_name, "expected a correlation_id derivedField on the Loki datasource"


def test_the_derived_field_extracts_only_correlation_id_never_an_identifier():
    """The privacy boundary this dashboard's Loki panel already assumes:
    only a categorical/opaque field may become a clickable link. A regex
    accidentally capturing a patient/user/draft id would turn a harmless
    log-correlation feature into an identifier-search tool."""
    loki = _loki_datasource()
    field = next(f for f in loki["jsonData"]["derivedFields"] if f["name"] == "correlation_id")
    forbidden = ("patient", "user_id", "ssn", "ip_address", "draft_id", "insurance_id")
    assert not any(word in field["matcherRegex"].lower() for word in forbidden)
    assert not any(word in field["name"].lower() for word in forbidden)


def test_the_derived_field_links_back_into_loki_not_an_external_or_broken_datasource():
    """Must resolve to a datasource this stack actually runs today
    (loki_ds) — not a placeholder Tempo datasource this stage never
    provisions, which would just show as an unhealthy link."""
    loki = _loki_datasource()
    field = next(f for f in loki["jsonData"]["derivedFields"] if f["name"] == "correlation_id")
    known = _known_datasource_uids()
    assert field["datasourceUid"] in known
    assert field["datasourceUid"] == "loki_ds"
