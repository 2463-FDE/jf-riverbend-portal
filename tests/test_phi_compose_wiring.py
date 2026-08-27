"""w8-planner-2 P2, PR 3/4 — docker-compose.yml/.env.example/CI wiring for
the three PHI key env vars (adr/0012).

Runs real `docker compose config` invocations (needs the `docker` CLI, same
as tests/test_docker_compose_config.py-style checks elsewhere in this repo)
with a fully-isolated environment (`--env-file /dev/null` plus an explicit
env dict) — never the ambient test-runner environment or this worktree's
own `.env`, so these tests behave identically whether or not a developer
happens to have PHI_* already exported locally.
"""
import os
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Every var docker-compose.yml requires via ${VAR:?...} OTHER than the PHI
# ones this file is specifically about — a complete, valid baseline every
# test here starts from and then removes exactly one PHI var from.
_OTHER_REQUIRED = {
    "INTERNAL_SERVICE_TOKEN": "test-internal-token-well-over-the-32-char-floor",
    "DB_PASSWORD": "test-db-password",
    "DB_ADMIN_PASSWORD": "test-db-admin-password",
}

_VALID_PHI_ENV = {
    "PHI_ACTIVE_KEY_VERSION": "v1",
    "PHI_ENCRYPTION_KEY_V1": "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",  # 32 bytes, base64
    "PHI_BLIND_INDEX_KEY_V1": "OTg3NjU0MzIxMDk4NzY1NDMyMTA5ODc2NTQzMjEwOTg=",  # different 32 bytes
}


def _run_compose_config(env_overrides):
    env = dict(_OTHER_REQUIRED)
    env.update(_VALID_PHI_ENV)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    # PATH is required for `docker` itself to resolve.
    full_env = {"PATH": os.environ.get("PATH", "")}
    full_env.update(env)
    return subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "config", "-q"],
        cwd=REPO,
        env=full_env,
        capture_output=True,
        text=True,
    )


def _docker_available():
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")


def test_missing_phi_active_key_version_fails_compose():
    result = _run_compose_config({"PHI_ACTIVE_KEY_VERSION": None})
    assert result.returncode != 0
    assert "PHI_ACTIVE_KEY_VERSION" in result.stderr


def test_missing_phi_encryption_key_fails_compose():
    result = _run_compose_config({"PHI_ENCRYPTION_KEY_V1": None})
    assert result.returncode != 0
    assert "PHI_ENCRYPTION_KEY_V1" in result.stderr


def test_missing_phi_blind_index_key_fails_compose():
    result = _run_compose_config({"PHI_BLIND_INDEX_KEY_V1": None})
    assert result.returncode != 0
    assert "PHI_BLIND_INDEX_KEY_V1" in result.stderr


def test_valid_independent_phi_keys_allow_compose_configuration():
    result = _run_compose_config({})
    assert result.returncode == 0, result.stderr


# --- .env.example: placeholders only, no usable secret ----------------------


def test_env_example_lists_all_three_phi_vars_as_blank_placeholders():
    lines = (REPO / ".env.example").read_text().splitlines()
    found = {}
    for line in lines:
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        if key.strip() in _VALID_PHI_ENV:
            found[key.strip()] = value.strip()

    assert set(found) == set(_VALID_PHI_ENV), f"expected all three PHI vars in .env.example, found {sorted(found)}"
    for key, value in found.items():
        assert value == "", f"{key} must be a blank placeholder in .env.example, found {value!r}"


def test_env_example_documents_two_independent_key_generation_commands():
    text = (REPO / ".env.example").read_text()
    # The generation instructions must appear near the PHI block, and must
    # be shown as something a reader runs TWICE (independently) — not one
    # command whose output gets pasted into both vars.
    assert text.count("openssl rand -base64 32") >= 2, (
        "expected the base64-32-byte key generation command to appear at least "
        "twice (once per key) near the PHI_* vars in .env.example"
    )


def test_env_example_states_the_two_keys_must_differ():
    text = (REPO / ".env.example").read_text()
    phi_section_start = text.index("PHI_ACTIVE_KEY_VERSION")
    section = text[max(0, phi_section_start - 800) : phi_section_start + 400]
    assert re.search(r"different|independent", section, re.I), (
        ".env.example must explain that the encryption and blind-index keys must differ"
    )


# --- CI: every compose-required variable is wired in ------------------------


def test_ci_workflow_supplies_every_phi_variable():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    for var in _VALID_PHI_ENV:
        assert var in workflow, f"{var} is required by docker-compose.yml but never mentioned in ci.yml"


def test_ci_generates_independent_throwaway_phi_keys_and_pins_active_version_to_v1():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    # A loop over both key vars, each independently assigned its own
    # `openssl rand -base64 32` call (not one shared value copied to both) —
    # matches the pattern "for var in PHI_ENCRYPTION_KEY_V1 PHI_BLIND_INDEX_KEY_V1;
    # do ... export "$var=$(openssl rand -base64 32)" ... done".
    assert re.search(r"for var in[^\n]*PHI_ENCRYPTION_KEY_V1[^\n]*PHI_BLIND_INDEX_KEY_V1", workflow), (
        "expected PHI_ENCRYPTION_KEY_V1 and PHI_BLIND_INDEX_KEY_V1 to be generated "
        "independently in the same loop, not as two separately hand-written cases"
    )
    assert 'export "$var=$(openssl rand -base64 32)"' in workflow
    assert "export PHI_ACTIVE_KEY_VERSION=v1" in workflow


# --- backfill runs inside a container that actually has the keys -----------


def test_the_backfill_target_execs_into_a_service_that_receives_the_phi_keys():
    makefile = (REPO / "Makefile").read_text()
    backfill_match = re.search(r"^phi-backfill:.*\n(?:\t.*\n)+", makefile, re.M)
    assert backfill_match, "expected a phi-backfill target in the Makefile"
    backfill_body = backfill_match.group(0)

    exec_target_match = re.search(r"docker compose exec[^\n]*?\s+(\S+)\s+python3", backfill_body)
    assert exec_target_match, f"could not find the exec target service in: {backfill_body!r}"
    exec_target = exec_target_match.group(1)

    compose = (REPO / "docker-compose.yml").read_text()
    service_block_match = re.search(rf"^  {re.escape(exec_target)}:\n(?:(?:    .*)?\n)+", compose, re.M)
    assert service_block_match, f"no docker-compose.yml service block found for {exec_target!r}"
    service_block = service_block_match.group(0)

    for var in _VALID_PHI_ENV:
        assert f"${{{var}:?" in service_block, (
            f"phi-backfill execs into {exec_target!r}, but that service's compose block "
            f"does not require {var} — the backfill would run there with the key missing"
        )
