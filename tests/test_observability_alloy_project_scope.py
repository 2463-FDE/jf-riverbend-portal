"""W10 Final Stage 7 review fix OBS-M01 — Grafana Alloy discovers containers
via the host Docker socket, which is host-wide: without a project-scoped
keep rule, Alloy would tail every container on a shared dev machine, not
just this repo's own compose stack. These are pure text/parsing checks (no
docker/network access); the live cross-project isolation proof is a manual
smoke step (see docs/runbook.md), not part of this suite.
"""
import pathlib
import re

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ALLOY_CONFIG = _ROOT / "observability" / "alloy" / "config.alloy"
_COMPOSE = _ROOT / "docker-compose.yml"


def _alloy_text():
    return _ALLOY_CONFIG.read_text()


def test_a_project_label_keep_rule_exists():
    text = _alloy_text()
    assert '__meta_docker_container_label_com_docker_compose_project' in text, (
        "no rule keys off Compose's own project label — Alloy would discover "
        "every container on the host Docker socket, not just this project's"
    )
    # The keep rule must actually filter (action = "keep"), scoped to the
    # resolved project name reaching Alloy from its own environment — not a
    # hardcoded/guessed literal, which would silently stop matching the day
    # the project directory (and therefore the resolved project name) changes.
    keep_rule = re.search(
        r'rule\s*\{[^}]*__meta_docker_container_label_com_docker_compose_project[^}]*\}',
        text, re.DOTALL,
    )
    assert keep_rule, "could not find the full keep rule block"
    block = keep_rule.group(0)
    assert re.search(r'action\s*=\s*"keep"', block), "the project-label rule must be action = \"keep\", not a relabel"
    assert 'env("COMPOSE_PROJECT_NAME")' in block, (
        "the keep rule's regex must come from env(\"COMPOSE_PROJECT_NAME\"), "
        "not a literal project-name string"
    )


def test_loki_source_docker_consumes_the_filtered_relabel_output_not_raw_discovery():
    text = _alloy_text()
    source_block = re.search(r'loki\.source\.docker\s+"containers"\s*\{(.*?)\n\}', text, re.DOTALL)
    assert source_block, "could not find the loki.source.docker \"containers\" block"
    block = source_block.group(1)

    assert "discovery.relabel.containers.output" in block, (
        "loki.source.docker must consume discovery.relabel.containers.output "
        "(the project-filtered target list), not discovery.docker.containers.targets"
    )
    assert "discovery.docker.containers.targets" not in block, (
        "loki.source.docker still wires the UNFILTERED discovery.docker "
        "targets directly — the keep rule would have no effect on which "
        "containers are actually tailed"
    )


def test_the_bounded_service_and_correlation_id_behavior_is_unchanged():
    """OBS-M01 must not regress the existing label/structured-metadata
    bounds: `service` stays a label (bounded), `correlation_id` stays
    structured metadata (unbounded-safe), and no PHI redaction is added
    here — application logging remains that boundary."""
    text = _alloy_text()
    assert 'stage.labels' in text and 'service = ""' in text
    assert 'stage.structured_metadata' in text and 'correlation_id = ""' in text
    # Application logging remains the redaction boundary — Alloy must not
    # grow its own PHI-scrubbing stage (e.g. stage.replace, stage.drop) as a
    # side effect of this fix. Only check actual `stage.*` blocks, not
    # prose — the header comment legitimately explains that boundary.
    stage_kinds = set(re.findall(r'stage\.(\w+)\s*\{', text))
    assert stage_kinds == {"regex", "labels", "structured_metadata"}, (
        f"unexpected pipeline stage(s) added: {stage_kinds - {'regex', 'labels', 'structured_metadata'}}"
    )


def test_compose_passes_the_resolved_project_name_into_alloy():
    compose = yaml.safe_load(_COMPOSE.read_text())
    alloy_env = compose["services"]["alloy"].get("environment", {})
    assert "COMPOSE_PROJECT_NAME" in alloy_env, (
        "docker-compose.yml's alloy service does not forward "
        "COMPOSE_PROJECT_NAME — env(\"COMPOSE_PROJECT_NAME\") in "
        "config.alloy would resolve empty, matching every/no container"
    )
    assert alloy_env["COMPOSE_PROJECT_NAME"] == "${COMPOSE_PROJECT_NAME}", (
        "must reference Compose's own resolved ${COMPOSE_PROJECT_NAME}, not "
        "a hardcoded literal project name"
    )
