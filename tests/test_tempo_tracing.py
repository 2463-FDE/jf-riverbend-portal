"""W10 metrics Stage 3 — the local OTLP-HTTP -> otel-collector -> Tempo ->
Grafana trace path.

Static checks (compose/config graph, span names, attribute-key safety) run
unconditionally, matching the repo's own convention: no real `opentelemetry`
install is required, mirroring tests/test_tracing.py's own fake-SDK pattern
so this test file behaves identically in and out of CI.

The actual end-to-end proof — one real synthetic span reaching Tempo through
the real Collector — was run by hand this session (not automated here):
`tempo`/`otel-collector` publish no host port by design (matching loki/
alloy's own precedent, and the compose-port-exposure guard test below), so a
host-side pytest process cannot reach them without extra network plumbing
this stage does not add. The commands and the two full trace payloads Tempo
returned (one for each exporter config, before and after the otlp_grpc
rename) are recorded in this PR's own description.
"""
import os
import pathlib
import re
import subprocess
import sys
import types

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_DATASOURCES = _ROOT / "observability" / "grafana" / "provisioning" / "datasources" / "datasources.yml"
_COLLECTOR_CONFIG = _ROOT / "observability" / "otel-collector" / "otel-collector-config.yaml"
_TEMPO_CONFIG = _ROOT / "observability" / "tempo" / "tempo.yaml"

# Every file this stage added a safe_span/record_event call to.
_TRACED_SOURCES = (
    _ROOT / "libs" / "summary_agent" / "runtime.py",
    _ROOT / "libs" / "summary_agent" / "retrieval.py",
    _ROOT / "services" / "records-service" / "summary_agent_path.py",
    _ROOT / "libs" / "policy_navigator" / "runtime.py",
    _ROOT / "libs" / "policy_navigator" / "tool.py",
    _ROOT / "libs" / "eligibility_agent" / "runtimes" / "raw_bedrock.py",
    _ROOT / "services" / "eligibility-service" / "agent_wiring.py",
)

# The exact span names this stage introduces, one per surface stage — pins
# the set so a rename/removal is a deliberate, reviewed diff, not a silent one.
_EXPECTED_SPAN_NAMES = {
    "summary_agent.provider_call", "summary_agent.retrieval",
    "summary_agent_path.generate_draft", "summary_agent_path.validation",
    "policy_navigator.provider_call", "policy_navigator.retrieval", "policy_navigator.ask",
    "eligibility_agent.provider_call", "eligibility_agent.tool_call", "eligibility_agent.turn",
}

# Every attribute key this stage's spans/events are allowed to carry. A
# categorical/bounded value or an opaque, non-guessable id — never a
# patient/user/visit/account identifier, and never free text.
_ALLOWED_ATTRIBUTE_KEYS = {
    "correlation_id", "actor_role", "turn", "model", "stop_reason", "tool_name",
    "document_count", "outcome", "passed", "validation_code", "provenance_label",
    "termination_reason", "tool_called", "accepted",
}

# Identifier-shaped keys that must never appear as a literal dict key argument
# anywhere near a tracing call in these files — mirrors
# libs.agent_provenance.FORBIDDEN_KEYS's own patient/account-identifier
# exclusion, which libs.tracing's own redact() word-list does not separately
# repeat (see summary_agent_path.py's own comment on this).
_FORBIDDEN_IDENTIFIER_KEYS = (
    "patient_id", "user_id", "visit_id", "account_id", "session_id", "actor_id",
    "reviewed_by", "insurance_id", "draft_id", "ssn", "dob", "mrn",
)


def _compose_services():
    return yaml.safe_load(_COMPOSE.read_text())["services"]


# --- compose / config graph -------------------------------------------------


def test_tempo_and_otel_collector_are_wired_into_the_observability_profile():
    services = _compose_services()
    for name in ("tempo", "otel-collector"):
        assert name in services, f"{name} is missing from docker-compose.yml"
        assert services[name].get("profiles") == ["observability"], (
            f"{name} must be gated behind the observability profile, like prometheus/loki/alloy/grafana"
        )


def test_tempo_and_otel_collector_publish_no_host_port():
    """Matches loki/alloy's own precedent: neither is a human-facing UI, so
    neither needs (or should have) a published port. Also pinned by
    tests/test_compose_port_exposure.py's own classification."""
    services = _compose_services()
    for name in ("tempo", "otel-collector"):
        assert not services[name].get("ports"), f"{name} should not publish a host port"


def test_otel_collector_forwards_to_tempo_and_tempo_persists_locally():
    services = _compose_services()
    assert services["otel-collector"]["depends_on"] == ["tempo"]
    tempo_volumes = " ".join(services["tempo"]["volumes"])
    assert "tempo_data:/var/tempo" in tempo_volumes
    assert "tempo_data" in yaml.safe_load(_COMPOSE.read_text())["volumes"]


def test_grafana_depends_on_tempo_too():
    assert "tempo" in _compose_services()["grafana"]["depends_on"]


def test_records_service_and_eligibility_service_get_distinct_otel_service_names():
    """.env's own OTEL_SERVICE_NAME is one shared value passed to every
    service via env_file — without a per-service override, every traced
    service would show up under the same generic name in Tempo."""
    services = _compose_services()
    assert services["records-service"]["environment"]["OTEL_SERVICE_NAME"] == "records-service"
    assert services["eligibility-service"]["environment"]["OTEL_SERVICE_NAME"] == "eligibility-service"


# --- TRACE-B01 / TRACE-M01: the collector endpoint, correctly scoped ------
#
# TRACE-B01: intake-service, eligibility-service, and records-service were
# never given OTEL_EXPORTER_OTLP_ENDPOINT in docker-compose.yml at all —
# .env's own copy of that var is commented out by default, so every one of
# them silently fell back to ConsoleSpanExporter regardless of whether
# `make up-observability` was running.
#
# TRACE-M01: the first fix made the endpoint a literal, unconditional value —
# which then pointed all three at otel-collector even under plain `make up`,
# where that container never starts. `${OTEL_EXPORTER_OTLP_ENDPOINT:-}`
# resolves to empty by default; only `make up-observability` (via the
# Makefile, not `.env`) supplies the real value.
#
# Tested against the RESOLVED `docker compose config`, not the raw YAML: a
# real `docker compose config` invocation with a fully-isolated, test-only
# environment (mirrors tests/test_phi_compose_wiring.py's own established
# pattern) — never this worktree's own .env — so the assertion holds
# regardless of whether these values are later written as literals or as
# ${VAR:-default} interpolation.

_COMPOSE_REQUIRED_ENV = {
    "INTERNAL_SERVICE_TOKEN": "test-internal-token-well-over-the-32-char-floor",
    "DB_PASSWORD": "test-db-password",
    "DB_ADMIN_PASSWORD": "test-db-admin-password",
    "PHI_ACTIVE_KEY_VERSION": "v1",
    "PHI_ENCRYPTION_KEY_V1": "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",
    "PHI_BLIND_INDEX_KEY_V1": "OTg3NjU0MzIxMDk4NzY1NDMyMTA5ODc2NTQzMjEwOTg=",
}

_EXPECTED_TRACED_SERVICES = {
    "intake-service": "intake-service",
    "eligibility-service": "eligibility-service",
    "records-service": "records-service",
}

_COLLECTOR_ENDPOINT = "http://otel-collector:4318"


def _docker_available():
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _resolved_compose_config(extra_env=None):
    """The fully-resolved compose config, from a real `docker compose
    config` run against an isolated, test-only environment — never this
    worktree's own .env. `extra_env` simulates what `make up-observability`
    supplies on the command line, not through `.env`."""
    full_env = {"PATH": os.environ.get("PATH", "")}
    full_env.update(_COMPOSE_REQUIRED_ENV)
    full_env.update(extra_env or {})
    result = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "config"],
        cwd=_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")
def test_the_default_config_leaves_the_collector_endpoint_unset_for_all_three():
    """Plain `make up` (no override, no observability profile running) must
    resolve to the SAME falsy value libs/tracing's own `if otlp_endpoint:`
    check already treats as "not configured" — never a value pointing at a
    container this path never starts."""
    resolved = _resolved_compose_config()
    for service in _EXPECTED_TRACED_SERVICES:
        env = resolved["services"][service]["environment"]
        assert not env.get("OTEL_EXPORTER_OTLP_ENDPOINT"), (
            f"{service} should resolve OTEL_EXPORTER_OTLP_ENDPOINT to empty under plain `make up`, "
            f"got {env.get('OTEL_EXPORTER_OTLP_ENDPOINT')!r}"
        )
        # OTEL_SERVICE_NAME is unaffected by TRACE-M01 — still set unconditionally.
        assert env.get("OTEL_SERVICE_NAME") == _EXPECTED_TRACED_SERVICES[service]


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")
def test_the_observability_path_resolves_the_collector_endpoint_and_distinct_names():
    """Simulates exactly what `make up-observability` supplies on the
    command line (see the Makefile target) — never through `.env`."""
    resolved = _resolved_compose_config({"OTEL_EXPORTER_OTLP_ENDPOINT": _COLLECTOR_ENDPOINT})
    for service, expected_name in _EXPECTED_TRACED_SERVICES.items():
        env = resolved["services"][service]["environment"]
        assert env.get("OTEL_SERVICE_NAME") == expected_name, (
            f"{service} should resolve OTEL_SERVICE_NAME={expected_name!r}, got {env.get('OTEL_SERVICE_NAME')!r}"
        )
        assert env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == _COLLECTOR_ENDPOINT, (
            f"{service} should resolve OTEL_EXPORTER_OTLP_ENDPOINT={_COLLECTOR_ENDPOINT!r}, "
            f"got {env.get('OTEL_EXPORTER_OTLP_ENDPOINT')!r} — the collector's actual compose "
            f"service name is 'otel-collector', not 'collector'"
        )


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")
def test_untraced_services_get_no_otel_configuration_added():
    """Review scope boundary: gateway/scheduling/roi/interop must not gain
    tracing configuration as a side effect of this fix, in either mode."""
    for extra_env in (None, {"OTEL_EXPORTER_OTLP_ENDPOINT": _COLLECTOR_ENDPOINT}):
        resolved = _resolved_compose_config(extra_env)
        for service in ("gateway", "scheduling-service", "roi-service", "interop-service"):
            env = resolved["services"][service].get("environment") or {}
            assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env, (
                f"{service} should not receive OTEL_EXPORTER_OTLP_ENDPOINT"
            )
            assert "OTEL_SERVICE_NAME" not in env, f"{service} should not receive OTEL_SERVICE_NAME"


def test_make_up_observability_supplies_the_expected_collector_endpoint():
    """Pins the Makefile <-> compose relationship so they cannot silently
    drift apart: if the collector's compose service name or port ever
    changes, this must change too, deliberately, in the same diff."""
    makefile = (_ROOT / "Makefile").read_text()
    match = re.search(r"^up-observability:.*\n((?:\t.*\n)+)", makefile, re.M)
    assert match, "expected an up-observability target in the Makefile"
    body = match.group(1)
    assert f"OTEL_EXPORTER_OTLP_ENDPOINT={_COLLECTOR_ENDPOINT}" in body, (
        f"up-observability must supply OTEL_EXPORTER_OTLP_ENDPOINT={_COLLECTOR_ENDPOINT!r} "
        f"on the compose invocation itself — found: {body!r}"
    )
    assert "--profile observability" in body and " up" in body, (
        "up-observability must actually start the observability profile"
    )
    # The env var assignment must precede the same line's `docker compose`
    # invocation, not merely appear somewhere else in the target body.
    for line in body.splitlines():
        if "docker compose" in line and "--profile observability" in line:
            assert line.strip().startswith(f"OTEL_EXPORTER_OTLP_ENDPOINT={_COLLECTOR_ENDPOINT}"), (
                f"expected the env var set on the same line as the compose invocation: {line!r}"
            )
            break
    else:
        pytest.fail("no line in up-observability invokes `docker compose ... --profile observability up`")


def test_otel_collector_config_is_a_traces_only_pipeline():
    config = yaml.safe_load(_COLLECTOR_CONFIG.read_text())
    assert set(config["service"]["pipelines"]) == {"traces"}, (
        "this Collector must carry traces only — metrics stay on Prometheus's own scrape path"
    )
    pipeline = config["service"]["pipelines"]["traces"]
    assert pipeline["receivers"] == ["otlp"]
    # Not the bare "otlp" exporter alias — 0.159.0 warns it is deprecated
    # (verified against the running container's own startup log this session).
    assert pipeline["exporters"] == ["otlp_grpc"]
    assert "otlp_grpc" in config["exporters"]
    assert config["exporters"]["otlp_grpc"]["endpoint"] == "tempo:4317"


def test_tempo_config_receives_otlp_grpc_and_stores_locally():
    config = yaml.safe_load(_TEMPO_CONFIG.read_text())
    assert config["distributor"]["receivers"]["otlp"]["protocols"]["grpc"]["endpoint"] == "0.0.0.0:4317"
    assert config["storage"]["trace"]["backend"] == "local"
    # No metrics_generator: a service-graph feature needing a real
    # span-metrics pipeline (a Collector connector plus a Prometheus
    # remote_write target) this stage does not build — see tempo.yaml's own
    # header for why an unused, unverified toggle is worse than an absent one.
    assert "metrics_generator" not in config


def test_tempo_datasource_uses_the_documented_traces_to_logs_shape():
    """Verified against Grafana's own Tempo datasource docs before writing
    (Stage 2's review round 1 caught an unverified Loki-to-Loki link in this
    same file) — tracesToLogsV2 pointed at Loki is the documented shape, the
    `$` is escaped as `$$`, and the interpolation variable is the documented
    `${__span.tags.NAME}`, not an invented one."""
    config = yaml.safe_load(_DATASOURCES.read_text())
    tempo = next(ds for ds in config["datasources"] if ds["uid"] == "tempo_ds")
    links = tempo["jsonData"]["tracesToLogsV2"]
    assert links["datasourceUid"] == "loki_ds"
    assert links["customQuery"] is True
    assert "$${__span.tags.correlation_id}" in links["query"]
    assert links["tags"] == [{"key": "correlation_id"}]


# --- span names + attribute-key safety (static) -----------------------------


def _safe_span_calls(text: str):
    """Every `safe_span(tracer, "name", ...)` call's literal span-name
    string in `text` — a plain regex, not a Python parser, but every call
    site in these files passes a literal string, never an f-string or a
    variable, so this is exact."""
    return re.findall(r'safe_span\(\s*_TRACER_NAME\s*,\s*"([^"]+)"', text)


def _dict_literal_keys(text: str):
    """Every bare `"key":` token anywhere in the file — deliberately broader
    than just tracing call sites, so a forbidden identifier introduced
    ANYWHERE in one of these files (not only inside a traced dict literal)
    still fails loudly rather than depending on this regex's own precision."""
    return set(re.findall(r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:', text))


def test_every_introduced_span_name_is_in_the_pinned_set():
    found = set()
    for path in _TRACED_SOURCES:
        found |= set(_safe_span_calls(path.read_text()))
    assert found == _EXPECTED_SPAN_NAMES, (
        f"span names changed: added={found - _EXPECTED_SPAN_NAMES} "
        f"removed={_EXPECTED_SPAN_NAMES - found} — update the pinned set if intentional"
    )


def test_no_traced_file_uses_a_forbidden_identifier_shaped_key():
    for path in _TRACED_SOURCES:
        keys = _dict_literal_keys(path.read_text())
        leaked = keys & set(_FORBIDDEN_IDENTIFIER_KEYS)
        assert not leaked, f"{path} uses forbidden identifier key(s) {leaked} near a dict literal"


def test_every_attribute_key_actually_used_is_in_the_allowlist():
    """The complement of the forbidden-key check: every key these files DO
    use for a span/event attribute must be one this test file already
    reviewed and named safe — a new key added later without updating
    _ALLOWED_ATTRIBUTE_KEYS fails here rather than silently shipping."""
    for path in _TRACED_SOURCES:
        text = path.read_text()
        # Attribute dicts passed to safe_span(...) or record_event(...) —
        # narrower than _dict_literal_keys, since this one must not flag
        # unrelated dict literals (e.g. VisitTurnResult(...) kwargs).
        for call in re.finditer(r'(?:safe_span\([^)]*?,\s*\{|record_event\([^,]+,\s*"[^"]+",\s*\{)', text):
            start = call.end() - 1
            depth, i = 0, start
            while i < len(text):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            block = text[start:i + 1]
            keys = set(re.findall(r'"([a-zA-Z_][a-zA-Z0-9_]*)"\s*:', block))
            unknown = keys - _ALLOWED_ATTRIBUTE_KEYS
            assert not unknown, f"{path} passes unreviewed attribute key(s) {unknown}: {block!r}"


# --- exporter behavior (the bug this stage found and fixed) -----------------


def _install_fake_otel_sdk(monkeypatch, *, otlp_exporter_cls=None):
    """Mirrors tests/test_tracing.py's own _install_fake_otel_sdk exactly —
    a fake opentelemetry + opentelemetry.sdk.* stack registered in
    sys.modules, no real package required."""
    fake_trace_mod = types.ModuleType("opentelemetry.trace")

    class _FakeSpanCM:
        def __enter__(self):
            return types.SimpleNamespace(set_attribute=lambda *a, **k: None, set_status=lambda *a, **k: None)

        def __exit__(self, *a):
            return False

    class _FakeTracer:
        def start_as_current_span(self, name):
            return _FakeSpanCM()

    fake_trace_mod.get_tracer = lambda name: _FakeTracer()
    fake_trace_mod.set_tracer_provider = lambda provider: None
    fake_trace_mod.Status = lambda code: code
    fake_trace_mod.StatusCode = types.SimpleNamespace(OK="OK", ERROR="ERROR")
    fake_otel_mod = types.ModuleType("opentelemetry")
    fake_otel_mod.trace = fake_trace_mod

    fake_resources_mod = types.ModuleType("opentelemetry.sdk.resources")
    fake_resources_mod.Resource = types.SimpleNamespace(create=lambda attrs: attrs)

    class _FakeTracerProvider:
        def __init__(self, resource=None):
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    fake_sdk_trace_mod = types.ModuleType("opentelemetry.sdk.trace")
    fake_sdk_trace_mod.TracerProvider = _FakeTracerProvider

    class _FakeBatchSpanProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    class _FakeConsoleSpanExporter:
        pass

    fake_export_mod = types.ModuleType("opentelemetry.sdk.trace.export")
    fake_export_mod.BatchSpanProcessor = _FakeBatchSpanProcessor
    fake_export_mod.ConsoleSpanExporter = _FakeConsoleSpanExporter

    fake_sdk_mod = types.ModuleType("opentelemetry.sdk")

    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk", fake_sdk_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.resources", fake_resources_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", fake_sdk_trace_mod)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace.export", fake_export_mod)

    if otlp_exporter_cls is not None:
        fake_otlp_mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        fake_otlp_mod.OTLPSpanExporter = otlp_exporter_cls
        monkeypatch.setitem(sys.modules, "opentelemetry.exporter", types.ModuleType("opentelemetry.exporter"))
        monkeypatch.setitem(
            sys.modules, "opentelemetry.exporter.otlp", types.ModuleType("opentelemetry.exporter.otlp")
        )
        monkeypatch.setitem(
            sys.modules, "opentelemetry.exporter.otlp.proto",
            types.ModuleType("opentelemetry.exporter.otlp.proto"),
        )
        monkeypatch.setitem(
            sys.modules, "opentelemetry.exporter.otlp.proto.http",
            types.ModuleType("opentelemetry.exporter.otlp.proto.http"),
        )
        monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", fake_otlp_mod)


@pytest.fixture(autouse=True)
def _reset_provider_configured_flag():
    from libs.tracing import spans as tracing_spans

    tracing_spans._provider_configured = False
    yield
    tracing_spans._provider_configured = False


def test_otlp_exporter_is_constructed_with_no_explicit_endpoint_argument(monkeypatch):
    """The regression this stage found via an actual end-to-end run, not
    assumed: OTLPSpanExporter(endpoint=<value>) makes the SDK use that
    string as the exact URL, bypassing its own OTEL_EXPORTER_OTLP_ENDPOINT
    handling — which is what appends the per-signal /v1/traces path. A real
    Collector 404'd on every export until this was fixed to call
    OTLPSpanExporter() with no arguments, letting the SDK read the env var
    itself. This test would have caught it: an `endpoint` kwarg reappearing
    here fails the assertion below."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    captured_kwargs = {}

    class _FakeOTLPSpanExporter:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            captured_kwargs["_positional_args"] = args

    _install_fake_otel_sdk(monkeypatch, otlp_exporter_cls=_FakeOTLPSpanExporter)

    from libs.tracing.spans import safe_span

    with safe_span("svc", "op"):
        pass

    assert captured_kwargs.get("_positional_args") == ()
    assert "endpoint" not in captured_kwargs, (
        "OTLPSpanExporter must be constructed with no endpoint override so the SDK's own "
        "OTEL_EXPORTER_OTLP_ENDPOINT handling (which appends /v1/traces) actually runs"
    )
